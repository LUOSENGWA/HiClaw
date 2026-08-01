#!/bin/bash
# test-28-migration-e2e.sh
# E2E-validates the legacy CoPaw → QwenPaw runtime-state migration.
#
# Background: existing installs of HiClaw/AgentTeams that use CoPaw as the
# Manager runtime keep runtime state in ~/.copaw and ~/.copaw.secret
# (master_key, providers.json, envs.json). On upgrade to QwenPaw 2.0 the
# Manager must migrate ALL of that state without data loss. The migration
# script is copy-then-verify: .copaw-migrated is written only after every
# critical artifact is verified in the target; a partial copy is NOT marked
# complete and retries on the next startup.
#
# This test runs against the REAL migrate-copaw-state.sh in the container,
# inside a throwaway HOME (/tmp/mig-e2e) so the live Manager state is never
# touched. NOTE: the Manager image exports QWENPAW_WORKING_DIR, so the test
# explicitly overrides QWENPAW_WORKING_DIR / QWENPAW_SECRET_DIR /
# WORKSPACE_DIR together with HOME — otherwise the migration would target
# the live working dir. Covers:
#   * full secret migration (master_key, providers.json, envs.json)
#   * sessions/plugins/models/custom_channels/memory/digest migration
#   * workspace (SOUL.md/AGENTS.md/agent.json) migration
#   * content equality (data preserved, not just file presence)
#   * .copaw-migrated marker only after full verification
#   * idempotency (marker present → skip)
#   * partial-copy failure → marker NOT written → retry succeeds

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/test-helpers.sh"

test_setup "28-migration-e2e"

_AGENT_CTR="${TEST_AGENT_CONTAINER:-${TEST_CONTROLLER_CONTAINER:-agentteams-controller}}"
TEST_HOME="/tmp/mig-e2e"
_MIG_ENV=(
    -e HOME="${TEST_HOME}"
    -e QWENPAW_WORKING_DIR="${TEST_HOME}/.qwenpaw"
    -e QWENPAW_SECRET_DIR="${TEST_HOME}/.qwenpaw.secret"
    -e WORKSPACE_DIR="${TEST_HOME}/.qwenpaw/workspaces/default"
)

# Guard: skip if Manager is not QwenPaw/CoPaw (e.g., openclaw shard)
_MANAGER_RUNTIME=$(docker exec "${_AGENT_CTR}" printenv AGENTTEAMS_MANAGER_RUNTIME 2>/dev/null || echo "openclaw")
if [ "${_MANAGER_RUNTIME}" != "qwenpaw" ] && [ "${_MANAGER_RUNTIME}" != "copaw" ]; then
    log_info "Manager runtime is ${_MANAGER_RUNTIME} — skipping migration E2E test"
    test_teardown "28-migration-e2e"
    test_summary
    exit 0
fi

# Migration script must be shipped in the Manager image
if ! docker exec "${_AGENT_CTR}" test -f /opt/agentteams/scripts/init/migrate-copaw-state.sh 2>/dev/null; then
    log_fail "migrate-copaw-state.sh not present in Manager container"
    test_teardown "28-migration-e2e"
    test_summary
    exit 1
fi

# ------------------------------------------------------------
# Setup legacy CoPaw state in a throwaway HOME
# ------------------------------------------------------------
log_section "Legacy CoPaw State Setup"

docker exec "${_AGENT_CTR}" bash -c 'set -e
HOME_DIR=/tmp/mig-e2e
rm -rf "${HOME_DIR}"
mkdir -p "${HOME_DIR}/.copaw/workspaces/default" \
         "${HOME_DIR}/.copaw/memory" \
         "${HOME_DIR}/.copaw/digest" \
         "${HOME_DIR}/.copaw/custom_channels" \
         "${HOME_DIR}/.copaw/plugins" \
         "${HOME_DIR}/.copaw/models" \
         "${HOME_DIR}/.copaw/sessions" \
         "${HOME_DIR}/.copaw.secret"
echo "{\"chats\":[]}" > "${HOME_DIR}/.copaw/chats.json"
echo "history-db-bytes" > "${HOME_DIR}/.copaw/history.db"
echo "{\"config\":true}" > "${HOME_DIR}/.copaw/config.json"
echo "memory-note" > "${HOME_DIR}/.copaw/memory/2026-08-01.md"
echo "digest-state" > "${HOME_DIR}/.copaw/digest/state.json"
echo "channel-cfg" > "${HOME_DIR}/.copaw/custom_channels/matrix.json"
echo "plugin-cfg" > "${HOME_DIR}/.copaw/plugins/example.json"
echo "model-cfg" > "${HOME_DIR}/.copaw/models/gpt.json"
echo "session-data" > "${HOME_DIR}/.copaw/sessions/user1.jsonl"
echo "MASTER_KEY_TEST_123" > "${HOME_DIR}/.copaw.secret/master_key"
echo "{\"providers\":[{\"id\":\"test\"}]}" > "${HOME_DIR}/.copaw.secret/providers.json"
echo "{\"envs\":{\"KEY\":\"VALUE\"}}" > "${HOME_DIR}/.copaw.secret/envs.json"
echo "# SOUL legacy" > "${HOME_DIR}/.copaw/workspaces/default/SOUL.md"
echo "# AGENTS legacy" > "${HOME_DIR}/.copaw/workspaces/default/AGENTS.md"
echo "{\"agent\":true}" > "${HOME_DIR}/.copaw/workspaces/default/agent.json"
echo "extra-workspace-file" > "${HOME_DIR}/.copaw/workspaces/default/extra.txt"
'
if [ $? -eq 0 ]; then
    log_pass "Legacy .copaw + .copaw.secret state created under ${TEST_HOME}"
else
    log_fail "Failed to create legacy CoPaw state"
    test_teardown "28-migration-e2e"
    test_summary
    exit 1
fi

# ------------------------------------------------------------
# Run migration (real production script)
# ------------------------------------------------------------
log_section "Run Migration"

if docker exec "${_MIG_ENV[@]}" "${_AGENT_CTR}" \
       bash /opt/agentteams/scripts/init/migrate-copaw-state.sh >/dev/null 2>&1; then
    log_pass "Migration script exited 0"
else
    log_fail "Migration script exited non-zero"
fi

if docker exec "${_MIG_ENV[@]}" "${_AGENT_CTR}" test -f "${TEST_HOME}/.qwenpaw/.copaw-migrated"; then
    log_pass ".copaw-migrated marker written"
else
    log_fail ".copaw-migrated marker written"
fi

# ------------------------------------------------------------
# Verify all artifacts migrated with content equality
# ------------------------------------------------------------
log_section "Artifact Migration Verification"

docker exec "${_MIG_ENV[@]}" "${_AGENT_CTR}" bash -c '
set -e
QW="${QWENPAW_WORKING_DIR}"
SECRET="${QWENPAW_SECRET_DIR}"
assert_file() {  # $1=desc, $2=path, $3=expected-content
    if [ -f "$2" ]; then
        if [ -n "$3" ] && [ "$(cat "$2")" != "$3" ]; then
            echo "CONTENT_MISMATCH:$1"
        else
            echo "OK:$1"
        fi
    else
        echo "MISSING:$1"
    fi
}
# top-level files
assert_file "chats.json"        "${QW}/chats.json"               "{\"chats\":[]}"
assert_file "history.db"        "${QW}/history.db"               "history-db-bytes"
assert_file "config.json"       "${QW}/config.json"              "{\"config\":true}"
# state dirs
assert_file "memory"            "${QW}/memory/2026-08-01.md"     "memory-note"
assert_file "digest"            "${QW}/digest/state.json"        "digest-state"
assert_file "custom_channels"   "${QW}/custom_channels/matrix.json" "channel-cfg"
assert_file "plugins"           "${QW}/plugins/example.json"     "plugin-cfg"
assert_file "models"            "${QW}/models/gpt.json"          "model-cfg"
assert_file "sessions"          "${QW}/sessions/user1.jsonl"     "session-data"
# secret (critical for credentials)
assert_file "secret/master_key"     "${SECRET}/master_key"       "MASTER_KEY_TEST_123"
assert_file "secret/providers.json" "${SECRET}/providers.json"   "{\"providers\":[{\"id\":\"test\"}]}"
assert_file "secret/envs.json"      "${SECRET}/envs.json"        "{\"envs\":{\"KEY\":\"VALUE\"}}"
# workspace
assert_file "workspace/SOUL.md"   "${QW}/workspaces/default/SOUL.md"    "# SOUL legacy"
assert_file "workspace/AGENTS.md" "${QW}/workspaces/default/AGENTS.md"  "# AGENTS legacy"
assert_file "workspace/agent.json" "${QW}/workspaces/default/agent.json" "{\"agent\":true}"
assert_file "workspace/extra.txt" "${QW}/workspaces/default/extra.txt"  "extra-workspace-file"
' > /tmp/test28-verify.txt 2>&1

_OK=true
while IFS= read -r _line; do
    case "${_line}" in
        OK:*)    log_pass "${_line#OK:} migrated with content equality" ;;
        MISSING:*) log_fail "${_line#MISSING:} missing after migration"; _OK=false ;;
        CONTENT_MISMATCH:*) log_fail "${_line#CONTENT_MISMATCH:} content mismatch (data loss!)"; _OK=false ;;
    esac
done < /tmp/test28-verify.txt

if [ "${_OK}" != "true" ]; then
    log_fail "Artifact migration verification failed"
else
    log_pass "All 14 artifacts migrated with content equality"
fi

# ------------------------------------------------------------
# Idempotency: second run skips, does not clobber
# ------------------------------------------------------------
log_section "Idempotency"

docker exec "${_MIG_ENV[@]}" "${_AGENT_CTR}" bash -c '
set -e
QW="${QWENPAW_WORKING_DIR}"
# mutate target so a re-run would be detectable
echo "MUTATED" > "${QW}/chats.json"
OUT=$(bash /opt/agentteams/scripts/init/migrate-copaw-state.sh 2>&1)
RC=$?
echo "${OUT}" | grep -q "already completed" && echo "SKIP_OK" || echo "SKIP_FAIL:${OUT}"
if [ "$(cat "${QW}/chats.json")" != "MUTATED" ]; then
    echo "CLOBBER"
else
    echo "NO_CLOBBER"
fi
exit ${RC}
' > /tmp/test28-idem.txt 2>&1
_IDEM_RC=$?

if [ "${_IDEM_RC}" -eq 0 ] && grep -q "SKIP_OK" /tmp/test28-idem.txt && grep -q "NO_CLOBBER" /tmp/test28-idem.txt; then
    log_pass "Idempotent: second run skipped and did not clobber existing state"
else
    log_fail "Idempotency violated (rc=${_IDEM_RC}): $(cat /tmp/test28-idem.txt | head -2)"
fi

# ------------------------------------------------------------
# Failure path: partial copy → marker NOT written → retry succeeds
# ------------------------------------------------------------
log_section "Failure Retry Semantics"

docker exec "${_MIG_ENV[@]}" "${_AGENT_CTR}" bash -c '
set -e
QW="${QWENPAW_WORKING_DIR}"
rm -f "${QW}/.copaw-migrated"
rm -rf "${QW}/memory"
# make the memory destination un-creatable: a regular file occupies the path
touch "${QW}/memory"
bash /opt/agentteams/scripts/init/migrate-copaw-state.sh >/dev/null 2>&1
RC1=$?
if [ "${RC1}" -eq 0 ]; then
    echo "FAIL: migration should have failed"
    exit 1
fi
if [ -f "${QW}/.copaw-migrated" ]; then
    echo "FAIL: marker written despite partial copy"
    exit 1
fi
echo "NO_MARKER_ON_FAILURE"
# fix the conflict and retry
rm -f "${QW}/memory"
bash /opt/agentteams/scripts/init/migrate-copaw-state.sh >/dev/null 2>&1
if [ ! -f "${QW}/.copaw-migrated" ]; then
    echo "FAIL: retry did not write marker"
    exit 1
fi
if [ "$(cat "${QW}/memory/2026-08-01.md")" != "memory-note" ]; then
    echo "FAIL: memory content lost after retry"
    exit 1
fi
echo "RETRY_OK"
' > /tmp/test28-fail.txt 2>&1
_FAIL_RC=$?

if [ "${_FAIL_RC}" -eq 0 ] && grep -q "NO_MARKER_ON_FAILURE" /tmp/test28-fail.txt && grep -q "RETRY_OK" /tmp/test28-fail.txt; then
    log_pass "Failure path: no marker on partial copy, retry succeeds with data intact"
else
    log_fail "Failure retry semantics broken (rc=${_FAIL_RC}): $(cat /tmp/test28-fail.txt | head -3)"
fi

# ------------------------------------------------------------
# Cleanup throwaway HOME
# ------------------------------------------------------------
docker exec "${_AGENT_CTR}" rm -rf "${TEST_HOME}" >/dev/null 2>&1

test_teardown "28-migration-e2e"
test_summary
