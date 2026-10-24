#!/usr/bin/env bash
# ============================================================================
# agentteams-upgrade.sh — safe, layered AgentTeams upgrade (P1)
#
# P1 scope: preflight / plan / backup / controller / status / --dry-run.
# Workers, manager, dashboard and memory are P2/P3.
#
# Safety:
#   - preflight is read-only and emits JSON;
#   - backup stops the controller for a cold kine copy, then restarts it;
#   - controller renames the old container, runs the new image with the old
#     ports/mounts/network/restart policy, health-gates it, and keeps the old
#     container for rollback;
#   - --dry-run prints every command without mutating;
#   - --yes is required for backup/controller; without it only the plan prints.
#
# Exit codes: 0 ok / 2 usage / 3 gate fail / 4 rollback failed.
# ============================================================================
set -u

EXIT_OK=0
EXIT_USAGE=2
EXIT_GATE=3
EXIT_ROLLBACK=4

PROG="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ENV_FILE="${AGENTTEAMS_ENV_FILE:-$HOME/agentteams-manager.env}"
BACKUP_DIR=""
STATE_FILE=""
TARGET=""
CONTROLLER_IMAGE=""
DRY_RUN=0
YES=0
JSON=0
TIMEOUT=300
LOG_FILE=""
DOCKER_CMD=""
CONTROLLER_NAME="agentteams-controller"
OLD_NAME=""

WARN_LIST=()
ERR_LIST=()

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
log() {
    local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
    if [ "${JSON}" = "1" ]; then
        printf '%s\n' "$msg" >&2
    else
        printf '%s\n' "$msg"
    fi
    if [ -n "${LOG_FILE}" ]; then
        printf '%s\n' "$msg" >> "${LOG_FILE}" 2>/dev/null || true
    fi
}

warn() {
    WARN_LIST+=("$*")
    log "WARN: $*"
}

err() {
    ERR_LIST+=("$*")
    log "ERROR: $*"
}

die() {
    log "ERROR: $*"
    exit "${2:-$EXIT_GATE}"
}

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n\r' '  '
}

json_list() {
    local first=1 item
    printf '['
    for item in "$@"; do
        [ "$first" = "1" ] || printf ','
        first=0
        printf '"%s"' "$(json_escape "$item")"
    done
    printf ']'
}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: ${PROG} <command> [options]

Commands:
  preflight     read-only checks; JSON with --json
  plan          print the upgrade plan
  backup        cold kine backup + env + image record + state
  controller    upgrade the controller only (rename/run + health gate + rollback)
  status        show the last state (JSON with --json)
  help          show this help

Options:
  --env-file PATH          default: \${AGENTTEAMS_ENV_FILE:-\$HOME/agentteams-manager.env}
  --backup-dir DIR         default: \$HOME/agentteams-upgrade-backups/<UTC>
  --state-file PATH        default: <backup-dir>/state.json
  --target VERSION         target version (recorded in the plan/state)
  --controller-image IMG   target controller image (required for controller)
  --dry-run                print commands; do not mutate
  --yes                    allow mutations (required for backup/controller)
  --json                   machine-readable output (preflight/status)
  --timeout SEC            health-gate timeout (default 300)
  --log-file PATH          default: <backup-dir>/upgrade.log
  -h, --help               show this help

Exit codes: 0 ok / 2 usage / 3 gate fail / 4 rollback failed.
EOF
}

resolve_paths() {
    if [ -z "${BACKUP_DIR}" ]; then
        if [ -n "${STATE_FILE}" ]; then
            BACKUP_DIR="$(dirname "${STATE_FILE}")"
        else
            BACKUP_DIR="${HOME}/agentteams-upgrade-backups/$(date -u +%Y%m%dT%H%M%SZ)"
        fi
    fi
    if [ -z "${STATE_FILE}" ]; then
        STATE_FILE="${BACKUP_DIR}/state.json"
    fi
    if [ -z "${LOG_FILE}" ]; then
        LOG_FILE="${BACKUP_DIR}/upgrade.log"
    fi
}

# ---------------------------------------------------------------------------
# Docker / inspect helpers
# ---------------------------------------------------------------------------
detect_docker() {
    if command -v docker >/dev/null 2>&1; then
        DOCKER_CMD="docker"
    elif command -v podman >/dev/null 2>&1; then
        DOCKER_CMD="podman"
    else
        die "neither docker nor podman found" "${EXIT_GATE}"
    fi
    if ! "${DOCKER_CMD}" info >/dev/null 2>&1; then
        die "container runtime is not running: ${DOCKER_CMD}" "${EXIT_GATE}"
    fi
}

container_exists() {
    "${DOCKER_CMD}" inspect "$1" >/dev/null 2>&1
}

inspect_fmt() {
    "${DOCKER_CMD}" inspect --format "$1" "$2" 2>/dev/null || true
}

read_env() {
    [ -f "${ENV_FILE}" ] || return 0
    grep "^$1=" "${ENV_FILE}" 2>/dev/null | head -n 1 | cut -d= -f2- | tr -d '\r'
}

data_mount() {
    inspect_fmt '{{range .Mounts}}{{if eq .Destination "/data"}}{{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}{{end}}{{end}}' "${CONTROLLER_NAME}"
}

ports_count() {
    inspect_fmt '{{len .HostConfig.PortBindings}}' "${CONTROLLER_NAME}"
}

mounts_count() {
    inspect_fmt '{{len .Mounts}}' "${CONTROLLER_NAME}"
}

mount_lines() {
    inspect_fmt '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Source}}|{{.Destination}}|{{.RW}}|{{.Propagation}}|{{.Driver}}{{println}}{{end}}' "${CONTROLLER_NAME}"
}

fs_type_of() {
    local path="$1" fs=""
    if command -v findmnt >/dev/null 2>&1; then
        fs="$(findmnt -T "${path}" -o FSTYPE -n 2>/dev/null | head -n 1 || true)"
    fi
    if [ -z "${fs}" ] && command -v stat >/dev/null 2>&1; then
        fs="$(stat -f -c %T "${path}" 2>/dev/null || true)"
    fi
    printf '%s' "${fs}"
}

is_remote_fs() {
    case "$1" in
        nfs|nfs4|cifs|smb|smb3|sshfs|fuse.sshfs|glusterfs|ceph|9p|virtiofs|afs|lustre|gpfs) return 0 ;;
        fuse.*) return 0 ;;
        *) return 1 ;;
    esac
}

mount_json() {
    local mtype="$1" mname="$2" msrc="$3" mdst="$4" mrw="$5" mprop="$6" mdriver="$7" mfs="$8" mremote="$9"
    local rw="false"
    [ "${mrw}" = "true" ] && rw="true"
    printf '{"type":"%s","name":"%s","source":"%s","destination":"%s","rw":%s,"propagation":"%s","driver":"%s","fs_type":"%s","remote":%s}' \
        "$(json_escape "${mtype}")" "$(json_escape "${mname}")" "$(json_escape "${msrc}")" "$(json_escape "${mdst}")" \
        "${rw}" "$(json_escape "${mprop}")" "$(json_escape "${mdriver}")" "$(json_escape "${mfs}")" "${mremote}"
}

container_running() {
    local v
    v="$(inspect_fmt '{{.State.Running}}' "${CONTROLLER_NAME}")"
    [ "${v}" = "true" ]
}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
write_state() {
    local step="$1" status="$2" now
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    mkdir -p "$(dirname "${STATE_FILE}")" 2>/dev/null || true
    {
        echo "STEP=${step}"
        echo "STATUS=${status}"
        echo "UPDATED_AT=${now}"
        echo "BACKUP_DIR=${BACKUP_DIR}"
        echo "CONTROLLER_IMAGE=${CONTROLLER_IMAGE}"
    } > "${STATE_FILE%.json}.env"
    printf '{"step":"%s","status":"%s","updated_at":"%s","backup_dir":"%s","controller_image":"%s"}\n' \
        "${step}" "${status}" "${now}" \
        "$(json_escape "${BACKUP_DIR}")" "$(json_escape "${CONTROLLER_IMAGE}")" \
        > "${STATE_FILE}"
}

# ---------------------------------------------------------------------------
# Health gate
# ---------------------------------------------------------------------------
healthz_ok() {
    "${DOCKER_CMD}" exec "${CONTROLLER_NAME}" sh -c '
        if command -v curl >/dev/null 2>&1; then
            curl -fsS http://127.0.0.1:8080/healthz
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- http://127.0.0.1:8080/healthz
        else
            agt get teams >/dev/null 2>&1
        fi
    ' >/dev/null 2>&1
}

agt_ok() {
    "${DOCKER_CMD}" exec "${CONTROLLER_NAME}" agt get teams >/dev/null 2>&1 \
        && "${DOCKER_CMD}" exec "${CONTROLLER_NAME}" agt get workers >/dev/null 2>&1
}

logs_ok() {
    local n
    n="$("${DOCKER_CMD}" logs --tail 200 "${CONTROLLER_NAME}" 2>&1 | grep -cE 'panic|fatal' || true)"
    [ "${n}" = "0" ]
}

wait_health() {
    local waited=0
    while [ "${waited}" -lt "${TIMEOUT}" ]; do
        if healthz_ok && agt_ok && logs_ok; then
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    return 1
}

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
cmd_preflight() {
    detect_docker
    resolve_paths

    local data_env data_mount_val data_dir data_source
    data_env="$(read_env AGENTTEAMS_DATA_DIR)"
    data_mount_val="$(data_mount)"

    if [ ! -f "${ENV_FILE}" ]; then
        err "env file not found: ${ENV_FILE}"
    fi

    if [ -n "${data_env}" ] && [ -n "${data_mount_val}" ] && [ "${data_env}" != "${data_mount_val}" ]; then
        err "data dir mismatch: env=${data_env} mounted=${data_mount_val}"
    fi
    if [ -n "${data_env}" ]; then
        data_dir="${data_env}"; data_source="env"
    elif [ -n "${data_mount_val}" ]; then
        data_dir="${data_mount_val}"; data_source="mounted"
    else
        data_dir="agentteams-data"; data_source="default"
        warn "cannot read AGENTTEAMS_DATA_DIR from env or container; defaulting to agentteams-data"
    fi

    local ctrl_exists="false" running="false" image="" ports=0 mounts=0 entry="" net="" restart=""
    if container_exists "${CONTROLLER_NAME}"; then
        ctrl_exists="true"
        image="$(inspect_fmt '{{.Config.Image}}' "${CONTROLLER_NAME}")"
        ports="$(ports_count)"
        mounts="$(mounts_count)"
        entry="$(inspect_fmt '{{json .Config.Entrypoint}}' "${CONTROLLER_NAME}")"
        net="$(inspect_fmt '{{.HostConfig.NetworkMode}}' "${CONTROLLER_NAME}")"
        restart="$(inspect_fmt '{{.HostConfig.RestartPolicy.Name}}' "${CONTROLLER_NAME}")"
        container_running && running="true"
    else
        err "controller container not found: ${CONTROLLER_NAME}"
    fi

    local old_env_count=0 file_env_count=0
    if [ "${ctrl_exists}" = "true" ]; then
        old_env_count="$(inspect_fmt '{{len .Config.Env}}' "${CONTROLLER_NAME}")"
        case "${old_env_count}" in ''|*[!0-9]*) old_env_count=0 ;; esac
        file_env_count="$(grep -c '^[A-Za-z_][A-Za-z0-9_]*=' "${ENV_FILE}" 2>/dev/null || true)"
        case "${file_env_count}" in ''|*[!0-9]*) file_env_count=0 ;; esac
        if [ "${old_env_count}" -gt 0 ] && [ "${file_env_count}" -gt 0 ] && [ "${old_env_count}" -ne "${file_env_count}" ]; then
            warn "env count differs: container=${old_env_count} env-file=${file_env_count}; verify --env-file"
        fi
    fi

    local mount_items="" mounts_json="[]" data_model="volume"
    local has_volume=0 has_bind=0 has_remote=0
    local mtype mname msrc mdst mrw mprop mdriver mfs mremote
    while IFS='|' read -r mtype mname msrc mdst mrw mprop mdriver; do
        [ -n "${mdst}" ] || continue
        mfs=""
        mremote="false"
        if [ "${mtype}" = "bind" ]; then
            mfs="$(fs_type_of "${msrc}")"
        elif [ "${mtype}" = "volume" ] && [ -d "${msrc}" ]; then
            mfs="$(fs_type_of "${msrc}")"
        fi
        if [ -n "${mfs}" ] && is_remote_fs "${mfs}"; then
            mremote="true"
            warn "mount ${mdst} uses remote fs ${mfs}; preserved as-is (latency/locking may differ)"
        fi
        case "${mtype}" in
            volume) has_volume=1 ;;
            bind) has_bind=1 ;;
            tmpfs) ;;
            *) warn "unknown mount type ${mtype} at ${mdst}" ;;
        esac
        [ "${mremote}" = "true" ] && has_remote=1
        mount_items="${mount_items}${mount_items:+,}$(mount_json "${mtype}" "${mname}" "${msrc}" "${mdst}" "${mrw}" "${mprop}" "${mdriver}" "${mfs}" "${mremote}")"
    done < <(mount_lines)
    mounts_json="[${mount_items}]"
    if [ "${has_remote}" = "1" ]; then
        data_model="remote"
    elif [ "${has_volume}" = "1" ] && [ "${has_bind}" = "1" ]; then
        data_model="mixed"
    elif [ "${has_bind}" = "1" ]; then
        data_model="bind"
    else
        data_model="volume"
    fi

    local s3_configured="false" s3_bucket="" s3_endpoint="" s3_backend="unknown" has_data_minio=0
    s3_bucket="$(read_env AGENTTEAMS_FS_BUCKET)"
    [ -n "${s3_bucket}" ] || s3_bucket="$(read_env AGENTTEAMS_MINIO_BUCKET)"
    [ -n "${s3_bucket}" ] && s3_configured="true"
    s3_endpoint="$(read_env AGENTTEAMS_FS_ENDPOINT)"
    [ -n "${s3_endpoint}" ] || s3_endpoint="$(read_env AGENTTEAMS_MINIO_ENDPOINT)"
    if mount_lines | cut -d'|' -f4 | grep -qx "/data/minio"; then
        has_data_minio=1
    fi
    case "${s3_endpoint}" in
        *"127.0.0.1:9000"*|*"localhost:9000"*|*"agentteams-controller:9000"*) s3_backend="embedded-minio" ;;
        "")
            if [ "${has_data_minio}" = "1" ]; then s3_backend="embedded-minio"; else s3_backend="unknown"; fi
            ;;
        *) s3_backend="external-s3" ;;
    esac
    if [ "${s3_backend}" = "external-s3" ] && [ "${has_data_minio}" = "1" ]; then
        warn "external S3 endpoint but /data/minio mount is present; verify the storage migration state"
    fi
    if [ "${s3_backend}" = "embedded-minio" ] && [ "${has_data_minio}" = "0" ]; then
        warn "embedded MinIO endpoint but no /data/minio mount; verify the data model"
    fi
    [ "${s3_backend}" = "unknown" ] && warn "storage backend unknown; set AGENTTEAMS_FS_ENDPOINT or check /data/minio"
    [ "${s3_configured}" = "true" ] || warn "S3 bucket not found in env; worker state may rely on local mounts"

    local free_kb=0 kine_kb=0 free_bytes=0 kine_bytes=0
    local check_dir="${BACKUP_DIR}"
    while [ ! -d "${check_dir}" ] && [ "${check_dir}" != "/" ]; do
        check_dir="$(dirname "${check_dir}")"
    done
    free_kb="$(df -Pk "${check_dir}" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
    case "${free_kb}" in ''|*[!0-9]*) free_kb=0 ;; esac
    free_bytes=$((free_kb * 1024))
    if [ "${running}" = "true" ]; then
        kine_kb="$("${DOCKER_CMD}" exec "${CONTROLLER_NAME}" du -sk /data/agentteams-controller 2>/dev/null | awk '{print $1}' || true)"
        case "${kine_kb}" in ''|*[!0-9]*) kine_kb=0 ;; esac
        kine_bytes=$((kine_kb * 1024))
        if [ "${kine_kb}" -gt 0 ] && [ "${free_kb}" -lt $((kine_kb * 2)) ]; then
            err "insufficient disk for kine backup: free=${free_kb}KB need>=${kine_kb}KB*2"
        fi
    else
        warn "controller is not running; kine size and agt checks are skipped"
    fi

    local teams=0 workers=0
    if [ "${running}" = "true" ]; then
        teams="$("${DOCKER_CMD}" exec "${CONTROLLER_NAME}" agt get teams 2>/dev/null | wc -l | tr -d ' ' || true)"
        workers="$("${DOCKER_CMD}" exec "${CONTROLLER_NAME}" agt get workers 2>/dev/null | wc -l | tr -d ' ' || true)"
        case "${teams}" in ''|*[!0-9]*) teams=0 ;; esac
        case "${workers}" in ''|*[!0-9]*) workers=0 ;; esac
        [ "${teams}" -gt 0 ] || err "agt get teams returned no rows"
        [ "${workers}" -gt 0 ] || err "agt get workers returned no rows"
    fi

    local soul_set=-1 soul_missing=-1 workers_json=""
    workers_json="$("${DOCKER_CMD}" exec "${CONTROLLER_NAME}" agt get workers -o json 2>/dev/null || true)"
    if [ -n "${workers_json}" ] && printf '%s' "${workers_json}" | grep -q '"soul"'; then
        soul_set="$(printf '%s' "${workers_json}" | grep -o '"soul"' | wc -l | tr -d ' ')"
        soul_missing="$(printf '%s' "${workers_json}" | grep -o '"soul"[[:space:]]*:[[:space:]]*""' | wc -l | tr -d ' ')"
    else
        warn "spec.soul audit skipped: agt does not expose the soul field"
    fi

    local ok="true"
    [ "${#ERR_LIST[@]}" -gt 0 ] && ok="false"

    if [ "${JSON}" = "1" ]; then
        local warnings_json errors_json
        if [ "${#WARN_LIST[@]}" -gt 0 ]; then warnings_json="$(json_list "${WARN_LIST[@]}")"; else warnings_json="[]"; fi
        if [ "${#ERR_LIST[@]}" -gt 0 ]; then errors_json="$(json_list "${ERR_LIST[@]}")"; else errors_json="[]"; fi
        printf '{"ok":%s,"env_file":"%s","data_dir":"%s","data_dir_source":"%s","controller":{"name":"%s","exists":%s,"image":"%s","ports":%s,"mounts":%s,"entrypoint":"%s","network":"%s","restart":"%s","running":%s},"data_model":"%s","mounts":%s,"s3":{"backend":"%s","configured":%s,"bucket":"%s","data_minio_mount":%s},"docker":{"cmd":"%s"},"disk_free_bytes":%s,"kine_bytes":%s,"workers":{"total":%s,"soul_set":%s,"soul_missing":%s},"teams":%s,"warnings":%s,"errors":%s}\n' \
            "${ok}" "$(json_escape "${ENV_FILE}")" "$(json_escape "${data_dir}")" "${data_source}" \
            "${CONTROLLER_NAME}" "${ctrl_exists}" "$(json_escape "${image}")" "${ports}" "${mounts}" \
            "$(json_escape "${entry}")" "$(json_escape "${net}")" "$(json_escape "${restart}")" "${running}" \
            "${data_model}" "${mounts_json}" "${s3_backend}" "${s3_configured}" "$(json_escape "${s3_bucket}")" "$([ "${has_data_minio}" = "1" ] && echo true || echo false)" \
            "${DOCKER_CMD}" "${free_bytes}" "${kine_bytes}" "${workers}" "${soul_set}" "${soul_missing}" "${teams}" \
            "${warnings_json}" "${errors_json}"
    else
        log "preflight: env=${ENV_FILE} data_dir=${data_dir} (${data_source})"
        log "controller: exists=${ctrl_exists} running=${running} image=${image} ports=${ports} mounts=${mounts}"
        log "docker: ${DOCKER_CMD}; teams=${teams} workers=${workers}"
    fi

    [ "${ok}" = "true" ] && return "${EXIT_OK}"
    return "${EXIT_GATE}"
}

# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
cmd_plan() {
    resolve_paths
    log "AgentTeams safe upgrade plan (P1)"
    log "  env file       : ${ENV_FILE}"
    log "  backup dir     : ${BACKUP_DIR}"
    log "  target version : ${TARGET:-<current>}"
    log "  controller img : ${CONTROLLER_IMAGE:-<current>}"
    log "  1. preflight (read-only)"
    log "  2. backup (kine cold copy + env + image record)"
    log "  3. controller (rename/run + health gate; old container retained)"
    log "  P2: workers (canary) / P3: manager, dashboard, memory"
}

# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
record_images() {
    local out="${BACKUP_DIR}/images.txt" name val
    : > "${out}"
    for name in agentteams-controller agentteams-manager agentteams-dashboard; do
        if container_exists "${name}"; then
            val="$(inspect_fmt '{{.Config.Image}} {{.Id}} {{index .RepoDigests 0}}' "${name}")"
            printf '%s: %s\n' "${name}" "${val}" >> "${out}"
        fi
    done
}

cmd_backup() {
    detect_docker
    resolve_paths

    if [ ! -f "${ENV_FILE}" ]; then
        die "env file not found: ${ENV_FILE}" "${EXIT_GATE}"
    fi
    if ! container_exists "${CONTROLLER_NAME}"; then
        die "controller container not found: ${CONTROLLER_NAME}" "${EXIT_GATE}"
    fi

    if [ "${DRY_RUN}" = "1" ]; then
        log "[dry-run] mkdir -p ${BACKUP_DIR} && chmod 700 ${BACKUP_DIR}"
        log "[dry-run] cp ${ENV_FILE} ${BACKUP_DIR}/agentteams-manager.env"
        log "[dry-run] ${DOCKER_CMD} stop ${CONTROLLER_NAME}"
        log "[dry-run] ${DOCKER_CMD} cp ${CONTROLLER_NAME}:/data/agentteams-controller/. ${BACKUP_DIR}/kine/"
        log "[dry-run] record image IDs/digests to ${BACKUP_DIR}/images.txt"
        log "[dry-run] ${DOCKER_CMD} start ${CONTROLLER_NAME}"
        log "[dry-run] write state ${STATE_FILE}"
        return "${EXIT_OK}"
    fi
    [ "${YES}" = "1" ] || die "backup requires --yes (or use --dry-run)" "${EXIT_USAGE}"

    mkdir -p "${BACKUP_DIR}" && chmod 700 "${BACKUP_DIR}" || die "cannot create ${BACKUP_DIR}" "${EXIT_GATE}"
    cp "${ENV_FILE}" "${BACKUP_DIR}/agentteams-manager.env" 2>/dev/null || warn "could not copy env file"

    local was_running=0
    if container_running; then
        was_running=1
        "${DOCKER_CMD}" stop "${CONTROLLER_NAME}" >/dev/null 2>&1 || true
    fi

    mkdir -p "${BACKUP_DIR}/kine"
    if ! "${DOCKER_CMD}" cp "${CONTROLLER_NAME}:/data/agentteams-controller/." "${BACKUP_DIR}/kine/" >/dev/null 2>&1; then
        [ "${was_running}" = "1" ] && "${DOCKER_CMD}" start "${CONTROLLER_NAME}" >/dev/null 2>&1 || true
        write_state "backup" "failed"
        die "docker cp kine failed" "${EXIT_GATE}"
    fi
    record_images

    if [ "${was_running}" = "1" ]; then
        "${DOCKER_CMD}" start "${CONTROLLER_NAME}" >/dev/null 2>&1 || true
        if ! wait_health; then
            warn "controller did not pass the health gate after backup; backup is complete"
        fi
    fi

    write_state "backup" "done"
    log "backup done: ${BACKUP_DIR}"
    return "${EXIT_OK}"
}

# ---------------------------------------------------------------------------
# controller
# ---------------------------------------------------------------------------
build_run_args() {
    RUN_ARGS=(-d --name "${CONTROLLER_NAME}")
    local net restart
    net="$(inspect_fmt '{{.HostConfig.NetworkMode}}' "${CONTROLLER_NAME}")"
    restart="$(inspect_fmt '{{.HostConfig.RestartPolicy.Name}}' "${CONTROLLER_NAME}")"
    [ -n "${net}" ] && RUN_ARGS+=(--network "${net}")
    [ -n "${restart}" ] && RUN_ARGS+=(--restart "${restart}")
    [ -f "${ENV_FILE}" ] && RUN_ARGS+=(--env-file "${ENV_FILE}")

    local p hostip hostport
    while read -r p hostip hostport; do
        [ -n "${p}" ] || continue
        if [ -n "${hostip}" ] && [ "${hostip}" != "0.0.0.0" ]; then
            RUN_ARGS+=(-p "${hostip}:${hostport}:${p}")
        elif [ -n "${hostport}" ]; then
            RUN_ARGS+=(-p "${hostport}:${p}")
        else
            RUN_ARGS+=(-p "${p}")
        fi
    done < <(inspect_fmt '{{range $p, $conf := .HostConfig.PortBindings}}{{range $conf}}{{println $p .HostIp .HostPort}}{{end}}{{end}}' "${CONTROLLER_NAME}")

    local mtype mname msrc mdst mrw mprop mdriver spec
    while IFS='|' read -r mtype mname msrc mdst mrw mprop mdriver; do
        [ -n "${mdst}" ] || continue
        case "${mtype}" in
            volume)
                spec="type=volume,src=${mname},dst=${mdst}"
                [ "${mrw}" = "false" ] && spec="${spec},readonly"
                [ -n "${mdriver}" ] && spec="${spec},volume-driver=${mdriver}"
                RUN_ARGS+=(--mount "${spec}")
                ;;
            bind)
                case "${msrc}" in
                    *,*) die "bind mount source contains a comma; run the controller step manually: ${msrc}" "${EXIT_GATE}" ;;
                esac
                spec="type=bind,src=${msrc},dst=${mdst}"
                [ "${mrw}" = "false" ] && spec="${spec},readonly"
                [ -n "${mprop}" ] && spec="${spec},bind-propagation=${mprop}"
                RUN_ARGS+=(--mount "${spec}")
                ;;
            tmpfs)
                RUN_ARGS+=(--mount "type=tmpfs,dst=${mdst}")
                ;;
            *)
                die "unsupported mount type: ${mtype} at ${mdst}" "${EXIT_GATE}"
                ;;
        esac
    done < <(mount_lines)

    RUN_ARGS+=("${CONTROLLER_IMAGE}")
}

rollback_controller() {
    log "rolling back controller"
    "${DOCKER_CMD}" stop "${CONTROLLER_NAME}" >/dev/null 2>&1 || true
    "${DOCKER_CMD}" rm "${CONTROLLER_NAME}" >/dev/null 2>&1 || true
    if ! "${DOCKER_CMD}" rename "${OLD_NAME}" "${CONTROLLER_NAME}" >/dev/null 2>&1; then
        write_state "controller" "rollback_failed"
        die "rollback failed: cannot rename ${OLD_NAME} back" "${EXIT_ROLLBACK}"
    fi
    "${DOCKER_CMD}" start "${CONTROLLER_NAME}" >/dev/null 2>&1 || true
    if wait_health; then
        write_state "controller" "rolled_back"
        log "rollback done: old controller is healthy"
        exit "${EXIT_GATE}"
    fi
    write_state "controller" "rollback_failed"
    die "rollback failed: old controller did not pass the health gate" "${EXIT_ROLLBACK}"
}

cmd_controller() {
    detect_docker
    resolve_paths

    [ -n "${CONTROLLER_IMAGE}" ] || die "--controller-image is required" "${EXIT_USAGE}"
    [ -f "${ENV_FILE}" ] || die "env file not found: ${ENV_FILE}" "${EXIT_GATE}"
    container_exists "${CONTROLLER_NAME}" || die "controller container not found: ${CONTROLLER_NAME}" "${EXIT_GATE}"

    local old_image ports mounts
    old_image="$(inspect_fmt '{{.Config.Image}}' "${CONTROLLER_NAME}")"
    ports="$(ports_count)"
    mounts="$(mounts_count)"
    case "${ports}" in ''|*[!0-9]*) ports=0 ;; esac
    case "${mounts}" in ''|*[!0-9]*) mounts=0 ;; esac
    if [ "${ports}" -eq 0 ]; then
        die "unsupported controller shape: no published ports; run the controller step manually" "${EXIT_GATE}"
    fi
    if [ "${mounts}" -eq 0 ]; then
        die "unsupported controller shape: no mounts; run the controller step manually" "${EXIT_GATE}"
    fi

    build_run_args

    if [ "${DRY_RUN}" = "1" ]; then
        log "[dry-run] ${DOCKER_CMD} stop ${CONTROLLER_NAME}"
        log "[dry-run] ${DOCKER_CMD} rename ${CONTROLLER_NAME} agentteams-controller-<old>-old"
        log "[dry-run] ${DOCKER_CMD} run ${RUN_ARGS[*]}"
        log "[dry-run] health gate: healthz + agt get teams/workers + logs panic|fatal=0"
        log "[dry-run] on failure: stop/rm new, rename old back, start, health-gate old"
        return "${EXIT_OK}"
    fi
    [ "${YES}" = "1" ] || die "controller requires --yes (or use --dry-run)" "${EXIT_USAGE}"

    OLD_NAME="agentteams-controller-$(printf '%s' "${old_image}" | tr '/:' '__')-old"
    if container_exists "${OLD_NAME}"; then
        OLD_NAME="${OLD_NAME}-$(date -u +%Y%m%dT%H%M%SZ)"
    fi

    "${DOCKER_CMD}" stop "${CONTROLLER_NAME}" >/dev/null 2>&1 || true
    if ! "${DOCKER_CMD}" rename "${CONTROLLER_NAME}" "${OLD_NAME}" >/dev/null 2>&1; then
        "${DOCKER_CMD}" start "${CONTROLLER_NAME}" >/dev/null 2>&1 || true
        die "cannot rename the old controller; nothing changed" "${EXIT_GATE}"
    fi

    if ! "${DOCKER_CMD}" run "${RUN_ARGS[@]}"; then
        rollback_controller
    fi
    if wait_health; then
        write_state "controller" "done"
        log "controller upgraded: ${CONTROLLER_IMAGE}; old container kept as ${OLD_NAME}"
        return "${EXIT_OK}"
    fi
    rollback_controller
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
cmd_status() {
    resolve_paths
    if [ ! -f "${STATE_FILE}" ]; then
        die "state file not found: ${STATE_FILE}" "${EXIT_USAGE}"
    fi
    if [ "${JSON}" = "1" ]; then
        cat "${STATE_FILE}"
        return "${EXIT_OK}"
    fi
    sed -n 's/^/  /p' "${STATE_FILE%.json}.env" 2>/dev/null || cat "${STATE_FILE}"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
COMMAND="${1:-help}"
[ $# -gt 0 ] && shift

while [ $# -gt 0 ]; do
    case "$1" in
        --env-file) ENV_FILE="$2"; shift 2 ;;
        --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
        --state-file) STATE_FILE="$2"; shift 2 ;;
        --target) TARGET="$2"; shift 2 ;;
        --controller-image) CONTROLLER_IMAGE="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --log-file) LOG_FILE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --yes) YES=1; shift ;;
        --json) JSON=1; shift ;;
        -h|--help) usage; exit "${EXIT_OK}" ;;
        *) die "unknown option: $1" "${EXIT_USAGE}" ;;
    esac
done

case "${COMMAND}" in
    preflight) cmd_preflight ;;
    plan) cmd_plan ;;
    backup) cmd_backup ;;
    controller) cmd_controller ;;
    status) cmd_status ;;
    help|-h|--help) usage ;;
    *) usage; exit "${EXIT_USAGE}" ;;
esac
