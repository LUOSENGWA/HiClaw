#!/bin/bash
# qwenpaw-worker-entrypoint.sh - QwenPaw Worker Agent container startup.
# Reads controller-injected environment variables and launches qwenpaw-worker.
#
# Environment variables:
#   AGENTTEAMS_WORKER_NAME   - Worker name (required)
#   AGENTTEAMS_FS_ENDPOINT   - MinIO/OSS endpoint (required in local mode)
#   AGENTTEAMS_FS_ACCESS_KEY - MinIO/OSS access key (required in local mode)
#   AGENTTEAMS_FS_SECRET_KEY - MinIO/OSS secret key (required in local mode)
#   AGENTTEAMS_RUNTIME       - "k8s" for controller-managed storage access
#   TZ                   - Timezone (optional)

set -e

source /opt/agentteams/scripts/lib/agentteams-env.sh

WORKER_NAME="${AGENTTEAMS_WORKER_NAME:?AGENTTEAMS_WORKER_NAME is required}"
WORKER_CR_NAME="${AGENTTEAMS_WORKER_CR_NAME:-${WORKER_NAME}}"
# Align with CoPaw/openclaw/hermes: HOME == workspace == MinIO mirror root.
# install_dir is its parent so install_dir/<name> == HOME.
INSTALL_DIR="/root/agentteams-fs/agents"
WORKER_HOME="${AGENTTEAMS_WORKER_HOME:-${INSTALL_DIR}/${WORKER_NAME}}"
CONSOLE_PORT="${AGENTTEAMS_CONSOLE_PORT:-8088}"

log() {
    echo "[agentteams-qwenpaw-worker $(date '+%Y-%m-%d %H:%M:%S')] $1"
}

if [ -n "${TZ}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    ln -sf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    echo "${TZ}" > /etc/timezone
    log "Timezone set to ${TZ}"
fi

if [ "${AGENTTEAMS_RUNTIME:-}" = "k8s" ]; then
    log "Kubernetes mode: FileSync resolves the storage alias from controller credentials"
    # CLI args are required by qwenpaw-worker. FileSync uses controller-issued
    # MC_HOST credentials for OSS, or these static credentials for MinIO.
    FS_ENDPOINT="${AGENTTEAMS_FS_ENDPOINT:-k8s-placeholder}"
    FS_ACCESS_KEY="${AGENTTEAMS_FS_ACCESS_KEY:-k8s}"
    FS_SECRET_KEY="${AGENTTEAMS_FS_SECRET_KEY:-k8s}"
    FS_BUCKET="${AGENTTEAMS_FS_BUCKET:-agentteams-storage}"
else
    FS_ENDPOINT="${AGENTTEAMS_FS_ENDPOINT:?AGENTTEAMS_FS_ENDPOINT is required}"
    FS_ACCESS_KEY="${AGENTTEAMS_FS_ACCESS_KEY:?AGENTTEAMS_FS_ACCESS_KEY is required}"
    FS_SECRET_KEY="${AGENTTEAMS_FS_SECRET_KEY:?AGENTTEAMS_FS_SECRET_KEY is required}"
    FS_BUCKET="${AGENTTEAMS_FS_BUCKET:-agentteams-storage}"
fi
log "  FS bucket: ${FS_BUCKET}"

mkdir -p "${WORKER_HOME}"
export HOME="${WORKER_HOME}"
export AGENTTEAMS_WORKER_HOME="${WORKER_HOME}"
export AGENTTEAMS_AGENT_NAME="${WORKER_NAME}"
export AGENTTEAMS_AGENT_ROLE="worker"
export AGENTTEAMS_AGENT_HOME="${WORKER_HOME}"

# Set QWENPAW_WORKING_DIR before starting; QwenPaw reads it during config/plugin setup.
export QWENPAW_WORKING_DIR="${WORKER_HOME}/.qwenpaw"
export AGENT_WORKSPACE="${QWENPAW_WORKING_DIR}/workspaces/default"
export QWENPAW_SECRET_DIR="${QWENPAW_SECRET_DIR:-${QWENPAW_WORKING_DIR}.secret}"
export QWENPAW_RUNNING_IN_CONTAINER=true
export QWENPAW_LOG_LEVEL="${QWENPAW_LOG_LEVEL:-info}"

# LLM stream stall detection (QwenPaw 2.2.0 #7150): first-content and idle
# timeouts are read from these env vars at process startup (upstream default
# 30s each; 0 disables). Local SGLang can starve a stream under concurrent
# load (8/26 QwenPaw001 false-kill precedent, lesson #386), so AgentTeams
# defaults both to 300s. Priority: native QWENPAW_LLM_STREAM_* vars (finer
# per-phase control) > AGENTTEAMS_LLM_STREAM_TIMEOUT_S (single knob) > 300.
AGENTTEAMS_LLM_STREAM_TIMEOUT_S="${AGENTTEAMS_LLM_STREAM_TIMEOUT_S:-300}"
export QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT="${QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT:-${AGENTTEAMS_LLM_STREAM_TIMEOUT_S}}"
export QWENPAW_LLM_STREAM_IDLE_TIMEOUT="${QWENPAW_LLM_STREAM_IDLE_TIMEOUT:-${AGENTTEAMS_LLM_STREAM_TIMEOUT_S}}"

# Configure LoongSuite observability plugin if tracing is enabled.
CMS_TRACES_ENABLED="$(echo "${AGENTTEAMS_CMS_TRACES_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
if [ "${CMS_TRACES_ENABLED}" = "true" ]; then
    log "Configuring CoPaw CMS plugin..."
    LOONGSUITE_DIR="${HOME}/.loongsuite"
    mkdir -p "${LOONGSUITE_DIR}"

    cat > "${LOONGSUITE_DIR}/bootstrap-config.json" <<EOF
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "${AGENTTEAMS_CMS_ENDPOINT}",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
  "OTEL_EXPORTER_OTLP_HEADERS": "x-arms-license-key=${AGENTTEAMS_CMS_LICENSE_KEY},x-arms-project=${AGENTTEAMS_CMS_PROJECT},x-cms-workspace=${AGENTTEAMS_CMS_WORKSPACE}",
  "OTEL_SERVICE_NAME": "${AGENTTEAMS_CMS_SERVICE_NAME:-agentteams-worker-${WORKER_NAME}}",
  "OTEL_SEMCONV_STABILITY_OPT_IN": "http,gen_ai_latest_experimental",
  "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_AND_EVENT",
  "LOONGSUITE_PYTHON_SITE_BOOTSTRAP": "true",
  "LOONGSUITE_PYTHON_SITE_BOOTSTRAP_LOG_SUCCESS": "false"
}
EOF
    log "CoPaw CMS plugin configured at ${LOONGSUITE_DIR}/bootstrap-config.json"
    export LOONGSUITE_PYTHON_SITE_BOOTSTRAP=true
    export LOONGSUITE_PYTHON_SITE_BOOTSTRAP_LOG_SUCCESS=false
fi

VENV="/opt/venv/qwenpaw"

CMD_ARGS=(
    --name "${WORKER_NAME}"
    --cr-name "${WORKER_CR_NAME}"
    --fs "${FS_ENDPOINT}"
    --fs-key "${FS_ACCESS_KEY}"
    --fs-secret "${FS_SECRET_KEY}"
    --fs-bucket "${FS_BUCKET}"
    --install-dir "${INSTALL_DIR}"
    --console-port "${CONSOLE_PORT}"
)

log "Starting qwenpaw-worker: ${WORKER_NAME}"
log "  Worker CR name: ${WORKER_CR_NAME}"
log "  FS endpoint: ${FS_ENDPOINT}"
log "  Install dir: ${INSTALL_DIR}"
log "  Worker home: ${WORKER_HOME}"
log "  QwenPaw working dir: ${QWENPAW_WORKING_DIR}"
log "  QwenPaw venv: ${VENV}"
log "  Console port: ${CONSOLE_PORT}"

# In-place runtime upgrade (two-phase + post-start health gate, design doc
# §4.8). Phase 1 (pre-restart) swaps the package and restarts the container,
# leaving pending.json on success. Phase 2 (boot-check, below, before boot)
# verifies the installed venv version: match -> pending RETAINED (the app is
# not up yet, so this is only a pre-check); mismatch -> boot-check rolls back
# to the old version. The post-start gate (after the app is up) is the real
# arbiter: it verifies the running app serves the target version and stays
# alive, removing pending on success and rolling back on failure — so a
# package that installed fine but whose app fails to start is recovered
# automatically. boot-check always exits 0 once handled (the container MUST
# boot; the result lives in the job state file). The post-start exit code is
# the VERDICT (this worker image has no supervisor, so the rollback's
# restart primitive is kill, not supervisorctl):
#   0 = verification passed -> keep the new runtime (foreground)
#   3 = recovered -> the rollback restored the old runtime; relaunch the
#       worker in the foreground against it (kill mode verifies at the next
#       boot's wrapper version gate)
#   other (4 = FATAL: verification AND its rollback failed, pending kept) ->
#       never read as "keep the new runtime": stop the app and exit
#       non-zero so the container supervisor surfaces the failure.
UPGRADE_SH=/opt/agentteams/scripts/upgrade_qwenpaw_runtime.sh
PENDING_UPGRADE="${QWENPAW_WORKING_DIR}/upgrade/pending.json"
POST_START=0
if [ -f "${PENDING_UPGRADE}" ] && [ -f "${UPGRADE_SH}" ]; then
    log "Pending in-place upgrade found: running boot-check before startup..."
    if bash "${UPGRADE_SH}" --phase boot-check --venv "${VENV}" --pending-file "${PENDING_UPGRADE}"; then
        log "Upgrade boot-check completed"
        # boot-check retained the pending file (venv matched the target), so
        # the running app still has to be verified before the upgrade counts
        # as a success.
        if [ -f "${PENDING_UPGRADE}" ]; then
            POST_START=1
        fi
    else
        log "WARNING: upgrade boot-check exited $?; continuing startup"
    fi
fi

# No pending upgrade left to verify: normal foreground start.
if [ "${POST_START}" -ne 1 ]; then
    exec "${VENV}/bin/qwenpaw-worker" "${CMD_ARGS[@]}"
fi

# Post-start verification: start the app in the background, run the health
# gate (version/readiness/settle), then branch on the orchestrator's exit
# code (the verdict — see the two-phase block above).
log "Starting qwenpaw-worker in the background for post-start upgrade verification..."
"${VENV}/bin/qwenpaw-worker" "${CMD_ARGS[@]}" &
APP_PID=$!
POST_START_RC=0
bash "${UPGRADE_SH}" --phase post-start --venv "${VENV}" \
    --pending-file "${PENDING_UPGRADE}" --restart-mode kill || POST_START_RC=$?
case "${POST_START_RC}" in
    0)
        log "Post-start verification passed; keeping the new runtime."
        wait "${APP_PID}"
        ;;
    3)
        # The rollback restored the old runtime (kill mode: the app child is
        # already dead and the wrapper exited with it; the next boot's
        # version gate is the final verifier). Serve the old runtime from
        # here, in the foreground.
        log "Post-start recovered to the old runtime; relaunching the worker in the foreground."
        kill "${APP_PID}" 2>/dev/null || true
        wait "${APP_PID}" 2>/dev/null || true
        exec "${VENV}/bin/qwenpaw-worker" "${CMD_ARGS[@]}"
        ;;
    *)
        log "FATAL: post-start verification and its rollback failed (rc=${POST_START_RC}); manual intervention required."
        kill "${APP_PID}" 2>/dev/null || true
        wait "${APP_PID}" 2>/dev/null || true
        exit 1
        ;;
esac
