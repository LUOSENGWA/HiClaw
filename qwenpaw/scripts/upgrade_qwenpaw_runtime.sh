#!/usr/bin/env bash
# ============================================================================
# upgrade_qwenpaw_runtime.sh — in-place QwenPaw runtime upgrade, v1
#
# Companion to check_qwenpaw_upgrade.py (pre-flight, checks C1-C4): the
# checker answers "can this deployment take version X?", this script answers
# "do it, with a rollback path" — the S3-S6 steps of the in-place upgrade
# lifecycle proposed in AgentTeams#1341 (end-to-end lifecycle comment,
# 2026-10-11).
#
# Phases (--phase, default full = the original single-process flow):
#   full         S0 -> S3 -> S3b -> S4 -> S5 -> S6 in one process
#   pre-restart  phase 1 of the two-phase model (the agentteams-upgrade
#                plugin spawns it DETACHED so it survives the app kill):
#                S0 full checks (C3 mandatory via --wheelhouse) -> S1 old
#                version -> S3 install -> S3b patch -> write pending.json
#                BEFORE the swap becomes visible -> S4 restart primitive ->
#                exit 0. Completion is owned by phase 2 (state file).
#   boot-check   phase 2 (entrypoint, before `exec qwenpaw-worker`; the app
#                is not up yet): no pending.json -> no-op exit 0; venv
#                version == target -> success + pending removed; otherwise
#                reinstall old + re-apply patch = rolled_back. Always exits
#                0 once handled (the container must boot; the result lives
#                in the state file). --force-rollback skips the version
#                check (host-side runbook).
#
# Steps (full phase):
#   S0  pre-flight   external checker (default: the co-located
#                    check_qwenpaw_upgrade.py); nonzero exit aborts before
#                    anything is installed (exit 2)
#   S3  package swap surgical reinstall into the runtime venv: uv
#                    `--reinstall-package qwenpaw` when uv is available, else
#                    venv pip `--force-reinstall --no-deps` (AgentTeams
#                    worker images ship no uv)
#   S4  restart      backend-specific primitive (see backends below); a
#                    failed restart is not rolled back immediately — the
#                    health gate below is the arbiter
#   S5  health gate  poll until the reported version equals the target
#                    (timeout Ns); on first match, hold a settle window
#                    (--settle, default 65s) verifying the runtime stays
#                    alive and the version stays put, to catch a delayed
#                    wrapper crash the raw version endpoint would not reveal;
#                    timeout/mismatch/settle-death triggers rollback
#   S6  rollback     install the recorded old version -> restart -> verify
#                    the old version is ready; success exit 3, failure exit 4
#                    with a loud FATAL banner (manual intervention)
#   S2' record       optional --record-file: JSON written ONLY after S5 is
#                    fully green ("verified, then recorded"); on rollback the
#                    record is written at the (restored) old version
#
# Backends — the actual restart primitive differs by deployment:
#   in-container (default): run this script inside the container. For images
#     whose supervisor respawns the app (QwenPaw runtime image: supervisord,
#     measured ~7-9s to ready): `supervisorctl restart app` (default) or
#     --restart-mode kill. Venv default: /app/venv.
#   docker (--container NAME): run on the host. The AgentTeams worker/manager
#     images exec the runtime as PID1 with no supervisor, so the restart
#     primitive is `docker restart NAME`: it keeps the writable layer (the
#     in-place swap survives) and re-runs the full init chain including the
#     MinIO config re-mirror (measured 30-90s to ready). Venv default:
#     /opt/venv/qwenpaw.
#
# Out of scope in v1 (tracked in the #1341 lifecycle design): S1 quiesce
# (stop dispatching before the upgrade — operator/Leader duty in v1), the
# MinIO-persisted applied-runtime record with a boot-time version guard
# (v1 keeps the record local via --record-file), and the
# k8s/sandbox OpenKruise spec.image in-place path (there the image update
# subsumes the package swap).
#
# Exit codes: 0=ok  2=pre-flight/usage failure (nothing installed)
#             3=rolled back  4=rollback failed (manual intervention needed)
#             (boot-check: always 0 once handled; the result is in the state file)
#
# Usage:
#   upgrade_qwenpaw_runtime.sh --target X.Y.Z [--checker PATH] [--skip-check]
#       [--container NAME] [--venv PATH] [--uv PATH] [--restart-mode supervisorctl|kill]
#       [--timeout 90] [--settle 65] [--record-file PATH] [--dry-run] [--selftest]
#   # two-phase (agentteams-upgrade plugin / entrypoint):
#   upgrade_qwenpaw_runtime.sh --target X.Y.Z --phase pre-restart \
#       --job-id upg-... --state-file .../upgrade-<job-id>.json \
#       --pending-file .../pending.json
#   upgrade_qwenpaw_runtime.sh --phase boot-check --pending-file .../pending.json \
#       --venv /opt/venv/qwenpaw [--force-rollback]
#
# Environment hooks (all overridable; --selftest stubs every external action):
#   UG_VERSION_CMD   print the current version (raw "X.Y.Z" or the JSON
#                    {"version":"X"} form returned by /api/version; both are
#                    handled)
#   UG_INSTALL_CMD   install command with a {target} placeholder
#   UG_RESTART_CMD   restart command
#   UG_CHECKER_CMD   checker command with a {target} placeholder
#   UG_ALIVE_CMD     liveness probe (docker backend: prints the container
#                    Running state); used by the S5 settle window to catch a
#                    delayed wrapper crash that the raw version endpoint
#                    (served by the app subprocess) would not reveal
#   UG_PRE_INSTALL_CMD pre-install hook (docker backend: starts a stopped
#                    container, since the rollback's `docker exec ... pip
#                    install` needs a running container; no-op in-container)
#   UG_WHEELHOUSE    wheel cache dir for the pre-restart phase's mandatory
#                    C3 gate (default $QWENPAW_WORKING_DIR/upgrade/wheelhouse)
#
# Red line: --selftest runs a fully stubbed battery and never touches a real
# deployment; --dry-run prints the resolved commands without executing them.
# ============================================================================
set -u

VERSION_ENDPOINT="http://127.0.0.1:8088/api/version"

# ---------- environment hooks (caller values are kept; real defaults resolved in resolve_defaults) ----------
UG_VERSION_CMD="${UG_VERSION_CMD:-}"
UG_INSTALL_CMD="${UG_INSTALL_CMD:-}"
UG_PATCH_CMD="${UG_PATCH_CMD:-}"
UG_RESTART_CMD="${UG_RESTART_CMD:-}"
UG_CHECKER_CMD="${UG_CHECKER_CMD:-}"
UG_VENV="${UG_VENV:-}"
UG_UV="${UG_UV:-}"
UG_ALIVE_CMD="${UG_ALIVE_CMD:-}"
UG_PRE_INSTALL_CMD="${UG_PRE_INSTALL_CMD:-}"

# ---------- CLI state ----------
TARGET=""
CHECKER=""
SKIP_CHECK=0
CONTAINER=""
VENV=""
RESTART_MODE="supervisorctl"
TIMEOUT=90
SETTLE=65
SETTLE_POLL="${UG_SETTLE_POLL:-5}"
RECORD_FILE=""
DRY_RUN=0
SELFTEST=0
BACKEND="in-container"
PHASE="full"
JOB_ID=""
STATE_FILE=""
PENDING_FILE=""
FORCE_ROLLBACK=0
WHEELHOUSE_DIR=""

log() { printf '[upgrade] %s\n' "$*"; }
err() { printf '[upgrade] ERROR: %s\n' "$*" >&2; }

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# Emit a version string from raw output:
#   bare version ("2.2.2b4") -> as-is
#   JSON form ('{"version":"2.2.2b4"}', the shape /api/version returns) -> extract the field
extract_version() {
  local raw="$1" v
  case "$raw" in
    \{*)
      if command -v python3 >/dev/null 2>&1; then
        v="$(printf '%s' "$raw" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if isinstance(d, dict) and isinstance(d.get("version"), str):
    print(d["version"])
else:
    sys.exit(1)' 2>/dev/null)" || v=""
        if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
      fi
      ;;
  esac
  printf '%s' "$raw"
}

# Run UG_VERSION_CMD and return the version string; failure/empty output -> nonzero
get_version() {
  local out
  out="$(bash -c "$UG_VERSION_CMD" 2>/dev/null)" || return 1
  out="$(trim "$out")"
  [ -n "$out" ] || return 1
  extract_version "$out"
  return 0
}

# $1=command template  $2=version -> print with every {target} substituted
sub_target() {
  printf '%s' "${1//\{target\}/$2}"
}

now_ts() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

# --- state file: single source of truth across container restarts ---------
# write_state KEY=VALUE ... merges JSON values into $STATE_FILE (atomic
# replace). A value is stored as JSON when parseable, else as a string.
# No-op when STATE_FILE is empty (the full phase keeps its record.json
# behaviour and is untouched).
write_state() {
  [ -n "$STATE_FILE" ] || return 0
  [ $# -gt 0 ] || return 0
  python3 - "$STATE_FILE" "$@" <<'PYEOF'
import json, os, sys, tempfile
path = sys.argv[1]
data = {}
if os.path.isfile(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}
for arg in sys.argv[2:]:
    if "=" not in arg:
        continue
    key, value = arg.split("=", 1)
    try:
        data[key] = json.loads(value)
    except Exception:
        data[key] = value
directory = os.path.dirname(os.path.abspath(path))
os.makedirs(directory, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".upgrade-", dir=directory)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, path)
PYEOF
}

# pending_get KEY -> print the value from $PENDING_FILE (empty when absent)
pending_get() {
  python3 - "$PENDING_FILE" "$1" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
    value = data.get(sys.argv[2], "")
    print("" if value is None else value)
except Exception:
    print("")
PYEOF
}

# write_pending JOB_ID OLD TARGET VENV -> atomic pending.json
write_pending() {
  python3 - "$PENDING_FILE" "$1" "$2" "$3" "$4" <<'PYEOF'
import datetime, json, os, sys, tempfile
job_id, old, target, venv = sys.argv[2:6]
data = {
    "job_id": job_id,
    "old": old,
    "target": target,
    "venv": venv,
    "started_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
path = sys.argv[1]
directory = os.path.dirname(os.path.abspath(path))
os.makedirs(directory, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=directory)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, path)
PYEOF
}

run_install() {
  local t="$1" cmd
  if [ -n "$UG_PRE_INSTALL_CMD" ]; then
    if ! bash -c "$UG_PRE_INSTALL_CMD"; then
      err "pre-install hook failed: $UG_PRE_INSTALL_CMD"
      return 1
    fi
  fi
  cmd="$(sub_target "$UG_INSTALL_CMD" "$t")"
  log "install: $cmd"
  bash -c "$cmd"
}

run_restart() {
  log "restart: $UG_RESTART_CMD"
  bash -c "$UG_RESTART_CMD"
}

# $1=expected version; 1 when the runtime is still alive (and version-stable)
# for the whole settle window. Polls every 5s. The wrapper's own version gate
# (qwenpaw_worker._wait_for_qwenpaw_api, 60s deadline) can kill the runtime
# AFTER the app already reports the right version, so the raw version endpoint
# alone is not a liveness proof - this window is.
settle_window() {
  local want="$1" settle="$2" now deadline v alive
  now="$(date +%s)"
  deadline=$(( now + settle ))
  log "health gate: settle window ${settle}s (detecting delayed wrapper crash)"
  while :; do
    sleep "$SETTLE_POLL"
    if [ -n "$UG_ALIVE_CMD" ]; then
      if ! alive="$(bash -c "$UG_ALIVE_CMD" 2>/dev/null)"; then
        log "health gate: settle: liveness probe failed (runtime gone)"
        return 1
      fi
      case "$alive" in
        true|True|TRUE|1|yes|Yes|running|Running) ;;
        *) log "health gate: settle: runtime not alive (state='$alive')"; return 1 ;;
      esac
    fi
    if ! v="$(get_version)"; then
      log "health gate: settle: version probe failed (runtime gone)"
      return 1
    fi
    if [ "$v" != "$want" ]; then
      log "health gate: settle: version drift '$v' != '$want'"
      return 1
    fi
    now="$(date +%s)"
    [ "$now" -ge "$deadline" ] && break
  done
  log "health gate: settle window ${settle}s held - runtime stable"
  return 0
}

# $1=expected version  $2=timeout seconds  $3=settle seconds (0 disables the window)
health_gate() {
  local want="$1" limit="$2" settle="${3:-0}" now deadline v last=""
  now="$(date +%s)"
  deadline=$(( now + limit ))
  log "health gate: waiting for version == '$want' (timeout ${limit}s)"
  while :; do
    if v="$(get_version)"; then
      last="$v"
      if [ "$v" = "$want" ]; then
        log "health gate: version='$v' matched"
        if [ "$settle" -gt 0 ] && ! settle_window "$want" "$settle"; then
          return 1
        fi
        log "health gate: version='$v' OK"
        return 0
      fi
    fi
    now="$(date +%s)"
    [ "$now" -ge "$deadline" ] && break
    sleep 1
  done
  log "health gate: timeout ${limit}s (want='$want', last='${last:-<unavailable>}')"
  return 1
}

fatal_rollback_failed() {
  local line="============================================================"
  {
    printf '\n%s\n' "$line"
    printf '!! FATAL: ROLLBACK FAILED - qwenpaw may be left in a broken state !!\n'
    printf '!! Manual intervention required: inspect the runtime venv, reinstall the old version by hand, restart, and re-check /api/version !!\n'
    printf '%s\n\n' "$line"
  } >&2
}

# $1=status (ok|rolledback)  $2=applied_version; written only when --record-file is set.
# Ordering discipline: called only after all-green (or after rollback re-verified all-green).
write_record() {
  [ -n "$RECORD_FILE" ] || return 0
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if ! printf '{"status":"%s","applied_version":"%s","target_version":"%s","backend":"%s","recorded_at":"%s"}\n' \
    "$1" "$2" "$TARGET" "$BACKEND" "$ts" > "$RECORD_FILE"; then
    err "WARNING: record file write failed: $RECORD_FILE (does not affect the upgrade result; record it manually)"
  else
    log "record: $RECORD_FILE (verified, then recorded)"
  fi
}

# $1=previous version; does not return (exits 3/4 internally)
do_rollback() {
  local old="$1"
  log "ROLLBACK: restoring $old (target was $TARGET)"
  if ! run_install "$old"; then
    fatal_rollback_failed
    err "rollback failed: installing old version $old did not succeed"
    echo "SUMMARY: status=rollback-failed"
    exit 4
  fi
  if ! run_restart; then
    fatal_rollback_failed
    err "rollback failed: post-rollback restart did not succeed"
    echo "SUMMARY: status=rollback-failed"
    exit 4
  fi
  if ! health_gate "$old" "$TIMEOUT" "$SETTLE"; then
    fatal_rollback_failed
    err "rollback failed: post-rollback health gate not reached (want='$old')"
    echo "SUMMARY: status=rollback-failed"
    exit 4
  fi
  log "ROLLBACK: restored to $old"
  write_record "rolledback" "$old"
  echo "SUMMARY: status=rolledback old=$old target=$TARGET"
  exit 3
}

# Main upgrade flow (exits 0/2/3/4 internally)
do_upgrade() {
  local start_ts old elapsed ccmd
  start_ts="$(date +%s)"

  # 1) record the previous version
  if ! old="$(get_version)"; then
    err "could not read the current version (UG_VERSION_CMD failed or printed nothing)"
    echo "SUMMARY: status=aborted reason=old-version-unknown"
    exit 2
  fi
  log "previous version: $old"
  log "target version: $TARGET"

  # 2) compatibility checker (S0)
  if [ -n "$CHECKER" ] && [ "$SKIP_CHECK" -eq 0 ]; then
    ccmd="$(sub_target "$UG_CHECKER_CMD" "$TARGET")"
    log "checker: $ccmd"
    if ! bash -c "$ccmd"; then
      err "checker refused the upgrade (nonzero exit) - aborted, nothing was installed"
      echo "SUMMARY: status=aborted reason=checker-failed"
      exit 2
    fi
    log "checker passed"
  fi

  # 3) install the target
  if ! run_install "$TARGET"; then
    err "install of $TARGET failed -> rolling back"
    do_rollback "$old"
  fi

  # 3b) apply compatibility patches to the freshly-installed runtime.
  #      The in-place swap installs a pristine wheel, which does not carry the
  #      driver-reload compat fix the 2.2.1 image bakes at build time; re-apply
  #      it so the upgraded runtime keeps the fixed behavior. The patch is a
  #      no-op when the target already has the fix integrated upstream.
  if [ -n "$UG_PATCH_CMD" ]; then
    pcmd="$(sub_target "$UG_PATCH_CMD" "$TARGET")"
    log "compat patch: $pcmd"
    if ! bash -c "$pcmd"; then
      err "compat patch failed -> rolling back"
      do_rollback "$old"
    fi
    log "compat patch done"
  fi

  # 4) restart (failure does not roll back directly; the health gate below decides)
  if ! run_restart; then
    err "restart command failed (no immediate rollback; the health gate below decides)"
  fi

  # 5) health gate
  if ! health_gate "$TARGET" "$TIMEOUT" "$SETTLE"; then
    do_rollback "$old"
  fi

  # 7) success
  elapsed=$(( $(date +%s) - start_ts ))
  write_record "ok" "$TARGET"
  echo "SUMMARY: status=ok old=$old target=$TARGET elapsed=${elapsed}s"
  exit 0
}

# ============================================================================
# Two-phase execution (agentteams-upgrade plugin / entrypoint).
# The full phase above is untouched; these reuse run_install / write_record
# / do_rollback-style steps but NOT run_restart+health_gate (boot-check) or
# S5 (pre-restart defers to phase 2).
# ============================================================================

# Phase 1 (spawned DETACHED by the plugin so it survives the app kill in S4):
#   S0 full checks (C3 mandatory via --wheelhouse) -> S1 old version ->
#   S3 install -> S3b patch -> write pending.json -> S4 restart -> exit 0.
# Exits: 0 = restart issued (phase 2 owns completion); 2 = pre-checks failed;
# 3 = rolled back; 4 = rollback itself failed.
do_pre_restart() {
  log "phase=pre-restart job=$JOB_ID target=$TARGET venv=$UG_VENV"

  # 1) record the previous version (the app is still up in its old form)
  local old
  if ! old="$(get_version)"; then
    err "could not read the current version (UG_VERSION_CMD failed or printed nothing)"
    echo "SUMMARY: status=precheck_failed reason=old-version-unknown"
    write_state state=precheck_failed finished_at="$(now_ts)"
    exit 2
  fi
  log "previous version: $old"

  # 2) compatibility checker (S0) - C3 mandatory (wheelhouse)
  if [ -n "$CHECKER" ] && [ "$SKIP_CHECK" -eq 0 ]; then
    ccmd="$(sub_target "$UG_CHECKER_CMD" "$TARGET")"
    log "checker: $ccmd"
    if ! bash -c "$ccmd"; then
      err "checker refused the upgrade (nonzero exit) - aborted, nothing was installed"
      echo "SUMMARY: status=precheck_failed reason=checker-failed"
      write_state state=precheck_failed finished_at="$(now_ts)"
      exit 2
    fi
    log "checker passed"
  fi

  # 3) install the target
  if ! run_install "$TARGET"; then
    err "install of $TARGET failed -> rolling back (no restart; app still runs the old code)"
    do_pre_restart_rollback "$old" "install-failed"
  fi
  log "install of $TARGET done"

  # 3b) re-apply the compat patch (idempotent)
  if [ -n "$UG_PATCH_CMD" ]; then
    pcmd="$(sub_target "$UG_PATCH_CMD" "$TARGET")"
    log "compat patch: $pcmd"
    if ! bash -c "$pcmd"; then
      err "compat patch failed -> rolling back (no restart; app still runs the old code)"
      do_pre_restart_rollback "$old" "patch-failed"
    fi
    log "compat patch done"
  fi

  # 4) write pending BEFORE the swap becomes visible to a new process
  write_pending "$JOB_ID" "$old" "$TARGET" "$UG_VENV"
  log "pending written: $PENDING_FILE (old=$old target=$TARGET)"
  write_state state=running phase=boot-check job_id="$JOB_ID" target="$TARGET" current="$old" started_at="$(now_ts)"

  # 5) restart primitive. After this point the container runtime brings the
  #    container back up and the entrypoint runs phase 2 (boot-check).
  log "S4: restart (mode=$RESTART_MODE)"
  if ! run_restart; then
    err "restart command failed; the container may still be running the old version"
    echo "SUMMARY: status=precheck_failed reason=restart-failed"
    write_state state=precheck_failed finished_at="$(now_ts)"
    exit 2
  fi
  log "pre-restart complete: restart issued; boot-check owns completion (state: $STATE_FILE)"
  echo "SUMMARY: status=ok phase=pre-restart old=$old target=$TARGET"
  exit 0
}

# pre-restart rollback: reinstall old + re-apply patch, NO restart (the app
# never died), then clean pending. Does not return (exits 3/4).
do_pre_restart_rollback() {
  local old="$1"
  local reason="${2:-install-failed}"
  log "ROLLBACK (pre-restart): restoring $old (reason: $reason; no restart - app unchanged)"
  write_state state=rolled_back finished_at="$(now_ts)" note="rollback:$reason"
  if ! run_install "$old"; then
    fatal_rollback_failed
    err "rollback failed: installing old version $old did not succeed"
    echo "SUMMARY: status=rollback_failed old=$old target=$TARGET"
    write_state state=rollback_failed
    exit 4
  fi
  if [ -n "$UG_PATCH_CMD" ]; then
    pcmd="$(sub_target "$UG_PATCH_CMD" "$old")"
    if ! bash -c "$pcmd"; then
      fatal_rollback_failed
      err "rollback failed: compat patch re-apply failed for $old"
      echo "SUMMARY: status=rollback_failed old=$old target=$TARGET"
      write_state state=rollback_failed
      exit 4
    fi
  fi
  rm -f "$PENDING_FILE"
  log "ROLLBACK (pre-restart): restored to $old"
  echo "SUMMARY: status=rolled_back old=$old target=$TARGET reason=$reason"
  exit 3
}

# Phase 2 (entrypoint, before `exec qwenpaw-worker`; the app is not up yet).
# Always returns 0 once handled - the container MUST boot; the result lives
# in the state file (success / rolled_back / rollback_failed).
do_boot_check() {
  log "phase=boot-check venv=$UG_VENV pending=$PENDING_FILE"

  if [ ! -f "$PENDING_FILE" ]; then
    log "boot-check: no pending upgrade ($PENDING_FILE missing), nothing to do"
    return 0
  fi

  local pending_job pending_old pending_target
  pending_job="$(pending_get job_id)"
  pending_old="$(pending_get old)"
  pending_target="$(pending_get target)"
  [ -n "$TARGET" ] || TARGET="$pending_target"
  [ -n "$JOB_ID" ] || JOB_ID="$pending_job"
  if [ -z "$STATE_FILE" ] && [ -n "$JOB_ID" ]; then
    # Derive the state file from the pending file's directory. The pending
    # lives at $WORKING/upgrade/pending.json and the job state at
    # $WORKING/upgrade/upgrade-<job>.json, so the state file is a sibling of
    # the pending. More robust than relying on $QWENPAW_WORKING_DIR (the
    # entrypoint sets it, but a standalone/manual boot-check may not).
    STATE_FILE="$(dirname "$PENDING_FILE")/upgrade-$JOB_ID.json"
  fi
  log "boot-check: pending job=$JOB_ID old=$pending_old target=$TARGET"

  if [ "$FORCE_ROLLBACK" = "1" ]; then
    log "boot-check: --force-rollback: rolling back regardless of the venv version"
    do_boot_check_rollback "$pending_old" "forced"
    return 0
  fi

  local boot_version
  boot_version="$(bash -c "$UG_VERSION_CMD" 2>/dev/null | tr -d '[:space:]')" || boot_version=""
  if [ -z "$boot_version" ]; then
    err "boot-check: could not read the venv version; assuming mismatch, rolling back"
    do_boot_check_rollback "$pending_old" "version-unreadable"
    return 0
  fi
  log "boot-check: venv reports qwenpaw==$boot_version (target $TARGET)"

  if [ "$boot_version" = "$TARGET" ]; then
    # venv version matches the target, but the app is not up yet: boot-check
    # runs before `exec qwenpaw-worker`. Do NOT remove pending or record
    # success here — a correctly installed package whose app then fails to
    # start would lose its recovery marker and any automatic health-triggered
    # rollback. Retain pending and mark the intermediate state; the
    # entrypoint's post-start health gate (version/readiness/settle) is the
    # arbiter and rolls back on startup failure.
    write_state state=boot-check-passed finished_at="" note="awaiting post-start health gate"
    log "boot-check: venv version matches target; pending RETAINED for post-start health gate"
    echo "SUMMARY: status=boot-check-passed phase=boot-check target=$TARGET"
    return 0
  fi

  err "boot-check: version mismatch (expected $TARGET, venv has $boot_version); rolling back to $pending_old"
  do_boot_check_rollback "$pending_old" "version-mismatch"
  return 0
}

# boot-check rollback: reinstall old + re-apply patch, NO restart and NO
# health gate (the app is not up; it cannot be verified before boot).
# pending is KEPT on failure (manual retry via --force-rollback).
do_boot_check_rollback() {
  local old="$1"
  local reason="${2:-version-mismatch}"
  log "ROLLBACK (boot-check): restoring $old (reason: $reason; no restart - app not up yet)"
  write_state state=rolled_back finished_at="$(now_ts)" note="rollback:$reason"
  if ! run_install "$old"; then
    fatal_rollback_failed
    err "boot-check: FATAL restore failed; pending kept for manual retry (--force-rollback)"
    echo "SUMMARY: status=rollback_failed old=$old target=$TARGET"
    write_state state=rollback_failed
    return 0
  fi
  if [ -n "$UG_PATCH_CMD" ]; then
    pcmd="$(sub_target "$UG_PATCH_CMD" "$old")"
    if ! bash -c "$pcmd"; then
      fatal_rollback_failed
      err "boot-check: FATAL compat patch re-apply failed; pending kept for manual retry"
      echo "SUMMARY: status=rollback_failed old=$old target=$TARGET"
      write_state state=rollback_failed
      return 0
    fi
  fi
  rm -f "$PENDING_FILE"
  log "boot-check: rolled back to $old; pending removed; continuing startup"
  echo "SUMMARY: status=rolled_back phase=boot-check old=$old target=$TARGET reason=$reason"
  return 0
}

# Post-start health gate (run by the entrypoint AFTER `qwenpaw-worker` is up).
# boot-check verified the installed venv version before boot; this verifies the
# RUNNING app actually serves the target version and stays alive (settle
# window). On success it removes pending and records success; on failure it
# rolls back (reinstall old + patch + restart) so a correctly installed
# package whose app then fails to start is recovered automatically instead of
# being stranded without a recovery marker or an automatic rollback.
do_post_start() {
  log "phase=post-start venv=$UG_VENV pending=$PENDING_FILE"
  if [ ! -f "$PENDING_FILE" ]; then
    log "post-start: no pending upgrade ($PENDING_FILE missing), nothing to do"
    return 0
  fi
  local pending_target pending_old
  pending_target="$(pending_get target)"
  pending_old="$(pending_get old)"
  [ -n "$TARGET" ] || TARGET="$pending_target"
  log "post-start: health gate for the running app, target=$TARGET"
  if health_gate "$TARGET" "$TIMEOUT" "$SETTLE"; then
    rm -f "$PENDING_FILE"
    write_state state=success finished_at="$(now_ts)"
    log "post-start: SUCCESS - health gate passed; pending removed"
    echo "SUMMARY: status=ok phase=post-start target=$TARGET"
    return 0
  fi
  err "post-start: health gate failed (want=$TARGET); rolling back to $pending_old"
  do_post_start_rollback "$pending_old" "post-start-health-failed"
  return 0
}

# post-start rollback: the app is up (possibly serving the new version), so
# restoring the old version needs a restart to take effect. pending is KEPT on
# failure (manual retry via --force-rollback).
do_post_start_rollback() {
  local old="$1"
  local reason="${2:-post-start-health-failed}"
  log "ROLLBACK (post-start): restoring $old (reason: $reason)"
  write_state state=rolled_back finished_at="$(now_ts)" note="rollback:$reason"
  if ! run_install "$old"; then
    fatal_rollback_failed
    err "post-start: FATAL restore failed; pending kept for manual retry (--force-rollback)"
    echo "SUMMARY: status=rollback_failed old=$old target=$TARGET"
    write_state state=rollback_failed
    return 0
  fi
  if [ -n "$UG_PATCH_CMD" ]; then
    pcmd="$(sub_target "$UG_PATCH_CMD" "$old")"
    if ! bash -c "$pcmd"; then
      fatal_rollback_failed
      err "post-start: FATAL compat patch re-apply failed; pending kept for manual retry"
      echo "SUMMARY: status=rollback_failed old=$old target=$TARGET"
      write_state state=rollback_failed
      return 0
    fi
  fi
  if [ -n "$UG_RESTART_CMD" ]; then
    rcmd="$(sub_target "$UG_RESTART_CMD" "$old")"
    if ! bash -c "$rcmd"; then
      fatal_rollback_failed
      err "post-start: FATAL restart after rollback failed; pending kept for manual retry"
      echo "SUMMARY: status=rollback_failed old=$old target=$TARGET"
      write_state state=rollback_failed
      return 0
    fi
  fi
  rm -f "$PENDING_FILE"
  log "post-start: rolled back to $old; pending removed"
  echo "SUMMARY: status=rolled_back phase=post-start old=$old target=$TARGET reason=$reason"
  return 0
}

# ============================================================================
# --selftest: built-in stubbed battery (11 scenarios; every external command mocked via tmp-dir state files)
# ============================================================================

SELFTEST_TOTAL_FAIL=0
SC_ID=""
SC_FAIL=0

st_begin() { # $1=id $2=description
  SC_FAIL=0
  log "selftest: --- $1 $2 ---"
}

st_check() { # $1=description $2=expected $3=actual
  if [ "$2" = "$3" ]; then
    printf '  [%s] %s: %s\n' "$SC_ID" "$1" "$3"
  else
    printf '  [%s] %s: expected=%s actual=%s\n' "$SC_ID" "$1" "$2" "$3"
    SC_FAIL=$((SC_FAIL+1))
  fi
}

st_grep() { # $1=description $2=file $3=pattern
  if grep -q "$3" "$2" 2>/dev/null; then
    printf '  [%s] %s: hit\n' "$SC_ID" "$1"
  else
    printf '  [%s] %s: miss (pattern=%s)\n' "$SC_ID" "$1" "$3"
    SC_FAIL=$((SC_FAIL+1))
  fi
}

st_end() { # $1=id $2=description
  if [ "$SC_FAIL" -eq 0 ]; then
    printf 'PASS: %s %s\n' "$1" "$2"
  else
    printf 'FAIL: %s %s (%d assertions unmet)\n' "$1" "$2" "$SC_FAIL"
    SELFTEST_TOTAL_FAIL=$((SELFTEST_TOTAL_FAIL+1))
  fi
}

run_selftest() {
  local tmp d rc
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/online_upgrade_v1.selftest.XXXXXX")" \
    || { err "cannot create temp dir"; return 1; }
  log "selftest: starting (11 scenarios, all external commands stubbed; tmp dir: $tmp)"
  # Keep the default scenarios fast; T8 exercises the settle window explicitly.
  SETTLE=0

  # ============ T1 all green ============
  d="$tmp/T1"; mkdir -p "$d"
  printf '2.2.1' > "$d/version"
  TARGET="2.2.2"; TIMEOUT=30; CHECKER=""; SKIP_CHECK=1
  RECORD_FILE="$d/record.json"
  UG_VERSION_CMD="cat $d/version"
  UG_INSTALL_CMD='printf %s {target} > '"$d"'/version'
  UG_RESTART_CMD='echo restart >> '"$d"'/restarts'
  ( do_upgrade ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T1"
  st_begin T1 "all green (2.2.1 -> install 2.2.2 -> restart -> health gate -> record)"
  st_check "exit" "0" "$rc"
  st_check "version file" "2.2.2" "$(cat "$d/version" 2>/dev/null)"
  st_check "restart count" "1" "$(wc -l < "$d/restarts" 2>/dev/null | tr -d '[:space:]')"
  st_grep "SUMMARY" "$d/out.log" "SUMMARY: status=ok old=2.2.1 target=2.2.2 elapsed="
  st_grep "record written" "$d/out.log" "verified, then recorded"
  if [ -f "$d/record.json" ] && grep -q '"applied_version":"2.2.2"' "$d/record.json" \
      && grep -q '"backend":"in-container"' "$d/record.json"; then
    printf 'rok' > "$d/rchk"
  else
    printf 'rbad' > "$d/rchk"
  fi
  st_check "record JSON" "rok" "$(cat "$d/rchk" 2>/dev/null)"
  st_end T1 "all green"

  # ============ T2 install fails -> rollback succeeds ============
  d="$tmp/T2"; mkdir -p "$d"
  printf '2.2.1' > "$d/version"
  TARGET="2.2.2"; TIMEOUT=30; CHECKER=""; SKIP_CHECK=1
  RECORD_FILE="$d/record.json"
  UG_VERSION_CMD="cat $d/version"
  # install stub succeeds only for the old version 2.2.1; target 2.2.2 -> nonzero (simulates a failed install)
  UG_INSTALL_CMD='[ "{target}" = "2.2.1" ] && printf %s {target} > '"$d"'/version'
  UG_RESTART_CMD='echo restart >> '"$d"'/restarts'
  ( do_upgrade ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T2"
  st_begin T2 "install fails -> rollback succeeds"
  st_check "exit" "3" "$rc"
  st_check "version file (restored)" "2.2.1" "$(cat "$d/version" 2>/dev/null)"
  # Arbitration note: task brief 168 T2 said "restart count x2", but per the flow definition
  # (install failure jumps straight to the rollback step, where the main-path restart is
  # never run) and per the S2 design (rollback = install old + restart + re-verify),
  # the whole run should be x1 (x2 is the T3 shape). Flow text wins; if a maintainer rules x2,
  st_check "restart count" "1" "$(wc -l < "$d/restarts" 2>/dev/null | tr -d '[:space:]')"
  st_grep "SUMMARY" "$d/out.log" "SUMMARY: status=rolledback old=2.2.1 target=2.2.2"
  st_grep "record written (rollback)" "$d/out.log" "verified, then recorded"
  if [ -f "$d/record.json" ] && grep -q '"status":"rolledback"' "$d/record.json" \
      && grep -q '"applied_version":"2.2.1"' "$d/record.json"; then
    printf 'rok' > "$d/rchk"
  else
    printf 'rbad' > "$d/rchk"
  fi
  st_check "record JSON (rollback = old version)" "rok" "$(cat "$d/rchk" 2>/dev/null)"
  st_end T2 "install fails -> rollback succeeds"

  # ============ T3 health gate mismatch -> rollback succeeds ============
  d="$tmp/T3"; mkdir -p "$d"
  printf '2.2.1' > "$d/version"
  TARGET="2.2.2"; TIMEOUT=3; CHECKER=""; SKIP_CHECK=1
  RECORD_FILE=""
  UG_VERSION_CMD="cat $d/version"
  UG_INSTALL_CMD='true'   # install "succeeds" but the version does not move (wrong/missing package)
  UG_RESTART_CMD='echo restart >> '"$d"'/restarts'
  ( do_upgrade ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T3"
  st_begin T3 "health gate mismatch (version stuck at 2.2.1) -> rollback succeeds"
  st_check "exit" "3" "$rc"
  st_check "version file (still old)" "2.2.1" "$(cat "$d/version" 2>/dev/null)"
  st_check "restart count (main path 1 + rollback 1)" "2" "$(wc -l < "$d/restarts" 2>/dev/null | tr -d '[:space:]')"
  st_grep "SUMMARY" "$d/out.log" "SUMMARY: status=rolledback old=2.2.1 target=2.2.2"
  st_end T3 "health gate mismatch -> rollback succeeds"

  # ============ T4 rollback also fails ============
  d="$tmp/T4"; mkdir -p "$d"
  printf '2.2.1' > "$d/version"
  TARGET="2.2.2"; TIMEOUT=3; CHECKER=""; SKIP_CHECK=1
  RECORD_FILE="$d/record.json"
  UG_VERSION_CMD="cat $d/version"
  UG_INSTALL_CMD='false'  # every install (including the rollback install) fails
  UG_RESTART_CMD='echo restart >> '"$d"'/restarts'
  ( do_upgrade ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T4"
  st_begin T4 "rollback also fails"
  st_check "exit" "4" "$rc"
  st_grep "SUMMARY" "$d/out.log" "SUMMARY: status=rollback-failed"
  st_grep "loud error" "$d/out.log" "FATAL: ROLLBACK FAILED"
  # rollback failure = unknown state -> no record is written (record semantics stay clean)
  if [ -e "$d/record.json" ]; then printf 'wrote' > "$d/rchk"; else printf 'absent' > "$d/rchk"; fi
  st_check "no record written" "absent" "$(cat "$d/rchk" 2>/dev/null)"
  st_end T4 "rollback also fails"

  # ============ T5 checker refuses ============
  d="$tmp/T5"; mkdir -p "$d"
  printf '2.2.1' > "$d/version"
  TARGET="2.2.2"; TIMEOUT=3; SKIP_CHECK=0
  RECORD_FILE=""
  CHECKER="/selftest/checker.py"   # checker given -> the check step runs
  UG_VERSION_CMD="cat $d/version"
  UG_INSTALL_CMD='echo "install {target}" >> '"$d"'/installs'
  UG_RESTART_CMD='echo restart >> '"$d"'/restarts'
  UG_CHECKER_CMD='false'           # checker refuses
  ( do_upgrade ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T5"
  st_begin T5 "checker refuses (UG_CHECKER_CMD=false)"
  st_check "exit" "2" "$rc"
  st_check "install log empty (no install happened)" "" "$(cat "$d/installs" 2>/dev/null)"
  st_check "version file unchanged" "2.2.1" "$(cat "$d/version" 2>/dev/null)"
  st_grep "SUMMARY" "$d/out.log" "SUMMARY: status=aborted reason=checker-failed"
  st_end T5 "checker refuses and nothing installs"

  # ============ T6 docker backend default command assembly (resolve layer, not executed) ============
  d="$tmp/T6"; mkdir -p "$d"
  (
    TARGET="2.2.2"; CONTAINER="agentteams-worker-demo"; VENV=""; TIMEOUT=90
    BACKEND="docker"; RESTART_MODE="supervisorctl"; CHECKER=""; SKIP_CHECK=0
    RECORD_FILE=""; UG_VERSION_CMD=""; UG_INSTALL_CMD=""; UG_RESTART_CMD=""
    UG_CHECKER_CMD=""; UG_VENV=""; UG_UV=""
    resolve_defaults
    {
      case "$UG_VERSION_CMD" in "docker exec agentteams-worker-demo"*) printf 'vok' ;; *) printf 'vbad' ;; esac
      case "$UG_INSTALL_CMD" in "docker exec agentteams-worker-demo"*) printf 'iok' ;; *) printf 'ibad' ;; esac
      case "$UG_RESTART_CMD" in "docker restart agentteams-worker-demo") printf 'rok' ;; *) printf 'rbad' ;; esac
      case "$UG_VERSION_CMD" in *"/opt/venv/qwenpaw"*) printf 'pok' ;; *) printf 'pbad' ;; esac
      case "$UG_INSTALL_CMD" in *"--reinstall-package qwenpaw qwenpaw=={target}"*) printf 'uok' ;; *) printf 'ubad' ;; esac
      case "$UG_INSTALL_CMD" in *"--force-reinstall --no-deps qwenpaw=={target}"*) printf 'fok' ;; *) printf 'fbad' ;; esac
      case "$UG_CHECKER_CMD" in *"--target {target} --in-place") printf 'cok' ;; *) printf 'cbad' ;; esac
    } > "$d/asserts"
    exit 0
  ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T6"
  st_begin T6 "docker backend default command assembly"
  st_check "exit" "0" "$rc"
  st_check "seven assertions" "vokiokrokpokuokfokcok" "$(cat "$d/asserts" 2>/dev/null)"
  st_end T6 "docker backend default command assembly"

  # ============ T7 docker backend full stubbed flow + record ============
  d="$tmp/T7"; mkdir -p "$d"
  printf '2.2.1' > "$d/version"
  TARGET="2.2.2"; TIMEOUT=30; CHECKER=""; SKIP_CHECK=1; BACKEND="docker"
  RECORD_FILE="$d/record.json"
  UG_VERSION_CMD="cat $d/version"
  UG_INSTALL_CMD='printf %s {target} > '"$d"'/version'
  UG_RESTART_CMD='echo restart >> '"$d"'/restarts'
  ( do_upgrade ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T7"
  st_begin T7 "docker backend full stubbed flow + record written"
  st_check "exit" "0" "$rc"
  st_check "version file" "2.2.2" "$(cat "$d/version" 2>/dev/null)"
  st_grep "SUMMARY" "$d/out.log" "SUMMARY: status=ok old=2.2.1 target=2.2.2 elapsed="
  st_grep "record written" "$d/out.log" "verified, then recorded"
  if [ -f "$d/record.json" ] && grep -q '"status":"ok"' "$d/record.json" \
      && grep -q '"applied_version":"2.2.2"' "$d/record.json" \
      && grep -q '"backend":"docker"' "$d/record.json"; then
    printf 'rok' > "$d/rchk"
  else
    printf 'rbad' > "$d/rchk"
  fi
  st_check "record JSON (docker)" "rok" "$(cat "$d/rchk" 2>/dev/null)"
  st_end T7 "docker backend full stubbed flow + record written"

  # ============ T8 settle window catches a delayed wrapper crash -> rollback ============
  # The app reports the right version immediately, but the wrapper (whose own
  # version gate outlives the app) dies a few seconds later. The raw version
  # endpoint alone would pass; the settle window's liveness probe must not.
  d="$tmp/T8"; mkdir -p "$d"
  printf '2.2.1' > "$d/version"
  TARGET="2.2.2"; TIMEOUT=30; CHECKER=""; SKIP_CHECK=1; BACKEND="docker"
  SETTLE=10; SETTLE_POLL=1; RECORD_FILE=""
  UG_VERSION_CMD="cat $d/version"
  UG_INSTALL_CMD='printf %s {target} > '"$d"'/version'
  UG_RESTART_CMD='c=$(cat '"$d"'/rc 2>/dev/null || echo 0); c=$((c+1)); echo $c > '"$d"'/rc; echo r >> '"$d"'/restarts'
  # Alive: stay up during the first settle window until the 3rd poll, then die
  # (rc==1); stay up for the whole post-rollback settle window (rc>=2).
  UG_ALIVE_CMD='rc=$(cat '"$d"'/rc 2>/dev/null || echo 0); a=$(cat '"$d"'/ac 2>/dev/null || echo 0); a=$((a+1)); echo $a > '"$d"'/ac; if [ "$rc" -eq 1 ] && [ $a -ge 3 ]; then echo false; else echo true; fi'
  ( do_upgrade ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T8"
  st_begin T8 "settle window catches delayed wrapper crash -> rollback"
  st_check "exit" "3" "$rc"
  st_check "version restored" "2.2.1" "$(cat "$d/version" 2>/dev/null)"
  st_grep "settle detected" "$d/out.log" "health gate: settle"
  st_grep "rolled back" "$d/out.log" "SUMMARY: status=rolledback old=2.2.1 target=2.2.2"
  st_end T8 "settle window catches delayed wrapper crash -> rollback"

  # ============ T9 pre-restart happy path (two-phase, phase 1) ============
  # Order must be: install(target) -> patch -> pending write -> restart;
  # the state file ends with phase=boot-check.
  d="$tmp/T9"; mkdir -p "$d"
  printf '2.2.1' > "$d/version"
  TARGET="2.2.2"; CHECKER=""; SKIP_CHECK=1; BACKEND="in-container"
  RECORD_FILE=""
  PHASE="pre-restart"; JOB_ID="upg-selftest-t9"
  STATE_FILE="$d/state.json"; PENDING_FILE="$d/pending.json"
  UG_VENV="$d/venv"
  UG_VERSION_CMD="cat $d/version"
  UG_INSTALL_CMD='printf %s {target} > '"$d"'/version'
  UG_PATCH_CMD='echo patch-{target} >> '"$d"'/calls'
  UG_RESTART_CMD='echo restart >> '"$d"'/calls'
  ( do_pre_restart ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T9"
  st_begin T9 "pre-restart happy path (install -> patch -> pending -> restart)"
  st_check "exit" "0" "$rc"
  st_check "target installed" "2.2.2" "$(cat "$d/version" 2>/dev/null)"
  if grep -q '"old": "2.2.1"' "$d/pending.json" 2>/dev/null \
      && grep -q '"target": "2.2.2"' "$d/pending.json" 2>/dev/null \
      && grep -q '"job_id": "upg-selftest-t9"' "$d/pending.json" 2>/dev/null; then
    printf 'pok' > "$d/pchk"
  else
    printf 'pbad' > "$d/pchk"
  fi
  st_check "pending fields" "pok" "$(cat "$d/pchk" 2>/dev/null)"
  if grep -q '"phase": "boot-check"' "$d/state.json" 2>/dev/null \
      && grep -q '"state": "running"' "$d/state.json" 2>/dev/null; then
    printf 'sok' > "$d/schk"
  else
    printf 'sbad' > "$d/schk"
  fi
  st_check "state phase=boot-check" "sok" "$(cat "$d/schk" 2>/dev/null)"
  st_check "first call = patch" "patch-2.2.2" "$(head -n1 "$d/calls" 2>/dev/null | tr -d '[:space:]')"
  st_check "last call = restart" "restart" "$(tail -n1 "$d/calls" 2>/dev/null | tr -d '[:space:]')"
  st_grep "SUMMARY" "$d/out.log" "SUMMARY: status=ok phase=pre-restart old=2.2.1 target=2.2.2"
  st_end T9 "pre-restart happy path"

  # ============ T10 boot-check mismatch -> rollback (phase 2, no restart) ============
  d="$tmp/T10"; mkdir -p "$d"
  printf '2.2.1' > "$d/version"   # venv reports the OLD version: mismatch
  PHASE="boot-check"; TARGET=""; CHECKER=""; SKIP_CHECK=1; BACKEND="in-container"
  RECORD_FILE=""
  PENDING_FILE="$d/pending.json"; STATE_FILE="$d/state.json"
  UG_VENV="$d/venv"
  printf '{"job_id": "upg-selftest-t10", "old": "2.2.1", "target": "2.2.2", "venv": "/fake/venv"}\n' > "$d/pending.json"
  UG_VERSION_CMD="cat $d/version"
  UG_INSTALL_CMD='printf %s {target} > '"$d"'/version'
  UG_PATCH_CMD='echo patch >> '"$d"'/calls'
  UG_RESTART_CMD='echo RESTART-CALLED >> '"$d"'/calls; exit 1'
  ( do_boot_check ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T10"
  st_begin T10 "boot-check mismatch -> rollback (no restart)"
  st_check "exit (boot-check always 0 once handled)" "0" "$rc"
  st_check "version restored to old" "2.2.1" "$(cat "$d/version" 2>/dev/null)"
  if [ -e "$d/pending.json" ]; then printf 'present' > "$d/eck"; else printf 'absent' > "$d/eck"; fi
  st_check "pending removed after rollback" "absent" "$(cat "$d/eck" 2>/dev/null)"
  if grep -q '"state": "rolled_back"' "$d/state.json" 2>/dev/null; then printf 'sok' > "$d/schk"; else printf 'sbad' > "$d/schk"; fi
  st_check "state=rolled_back" "sok" "$(cat "$d/schk" 2>/dev/null)"
  if grep -q 'RESTART-CALLED' "$d/calls" 2>/dev/null; then printf 'present' > "$d/rck"; else printf 'absent' > "$d/rck"; fi
  st_check "restart never called" "absent" "$(cat "$d/rck" 2>/dev/null)"
  st_grep "SUMMARY" "$d/out.log" "SUMMARY: status=rolled_back phase=boot-check old=2.2.1 target=2.2.2 reason=version-mismatch"
  st_end T10 "boot-check mismatch -> rollback"

  # ============ T11 boot-check no-op (no pending.json) ============
  d="$tmp/T11"; mkdir -p "$d"
  PHASE="boot-check"; TARGET=""; CHECKER=""; BACKEND="in-container"
  PENDING_FILE="$d/pending.json"; STATE_FILE="$d/state.json"
  UG_VENV="$d/venv"
  UG_VERSION_CMD="true"; UG_INSTALL_CMD="true"; UG_RESTART_CMD="true"
  ( do_boot_check ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T11"
  st_begin T11 "boot-check no-op (no pending.json)"
  st_check "exit" "0" "$rc"
  st_grep "nothing to do" "$d/out.log" "nothing to do"
  st_end T11 "boot-check no-op"

  log "selftest: tmp dir kept at $tmp (not removed automatically)"
  if [ "$SELFTEST_TOTAL_FAIL" -eq 0 ]; then
    echo "SELFTEST: 11/11 PASS"
    return 0
  fi
  echo "SELFTEST: $SELFTEST_TOTAL_FAIL/11 scenarios FAIL"
  return 1
}

# ============================================================================
# arguments / entrypoint
# ============================================================================

usage() {
  cat <<'EOF'
upgrade_qwenpaw_runtime.sh — in-place QwenPaw runtime upgrade, v1
(S0 pre-flight + S3 package swap + S4 restart + S5 health gate + S6 rollback + S2' record;
 lifecycle design = AgentTeams#1341 comment, 2026-10-11)

Usage:
  upgrade_qwenpaw_runtime.sh --target X.Y.Z [options]
  upgrade_qwenpaw_runtime.sh --selftest

Options:
  --target X.Y.Z        target version (required unless --selftest; PEP440 forms like 2.2.2b4 supported)
  --checker PATH        compatibility checker script (default: check_qwenpaw_upgrade.py
                        next to this script; S0 is skipped with a warning when neither
                        --checker nor the default is found)
  --skip-check          skip the checker (only meaningful when --checker is given)
  --container NAME      docker backend: run on the host and exec/restart container NAME
                        (venv defaults to /opt/venv/qwenpaw; restart primitive =
                        docker restart NAME)
  --venv PATH           runtime venv (overrides the backend default)
  --uv PATH             uv binary (forces the uv install form; when unset the
                        install command auto-detects uv and falls back to venv pip)
  --restart-mode MODE   supervisorctl (in-container default) | kill
  --timeout N           health-gate timeout in seconds (default 90)
  --settle N            post-match settle window in seconds (default 65; 0
                        disables). After the version first matches, the runtime
                        must stay alive and version-stable for this long; it
                        catches a delayed wrapper crash the raw version
                        endpoint (served by the app subprocess) would not reveal
  --phase PH            full (default, original flow) | pre-restart | boot-check
  --job-id ID           job id (required for pre-restart; boot-check takes it
                        from pending.json when omitted)
  --state-file PATH     state file for pre-restart/boot-check (default
                        $QWENPAW_WORKING_DIR/upgrade/upgrade-<job-id>.json)
  --pending-file PATH   pending.json (default $QWENPAW_WORKING_DIR/upgrade/pending.json)
  --force-rollback      boot-check only: roll back even if the version matches
  --record-file PATH    write a JSON version record after a successful upgrade
                        (verified, then recorded)
  --dry-run             print the resolved commands only; no install/restart/health gate
                        (full phase only)
  --selftest            run the built-in stubbed battery (7 scenarios, no real actions)
  -h, --help            this help

Environment hooks (all overridable; selftest relies on this mechanism):
  UG_VERSION_CMD / UG_INSTALL_CMD({target}) / UG_RESTART_CMD / UG_CHECKER_CMD({target})
  UG_VENV (default /app/venv, /opt/venv/qwenpaw with --container) / UG_UV (uv path;
  when unset the install command auto-detects uv and falls back to venv pip)

Exit codes:
  0 upgraded   2 pre-flight/usage failure (nothing installed)   3 rolled back
  4 rollback failed (manual intervention needed)
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --target)
        [ $# -ge 2 ] || { err "--target missing value"; exit 2; }
        TARGET="$2"; shift 2 ;;
      --target=*)     TARGET="${1#*=}"; shift ;;
      --checker)
        [ $# -ge 2 ] || { err "--checker missing value"; exit 2; }
        CHECKER="$2"; shift 2 ;;
      --checker=*)    CHECKER="${1#*=}"; shift ;;
      --skip-check)   SKIP_CHECK=1; shift ;;
      --container)
        [ $# -ge 2 ] || { err "--container missing value"; exit 2; }
        CONTAINER="$2"; shift 2 ;;
      --container=*)  CONTAINER="${1#*=}"; shift ;;
      --venv)
        [ $# -ge 2 ] || { err "--venv missing value"; exit 2; }
        VENV="$2"; shift 2 ;;
      --venv=*)       VENV="${1#*=}"; shift ;;
      --uv)
        [ $# -ge 2 ] || { err "--uv missing value"; exit 2; }
        UG_UV="$2"; shift 2 ;;
      --uv=*)         UG_UV="${1#*=}"; shift ;;
      --record-file)
        [ $# -ge 2 ] || { err "--record-file missing value"; exit 2; }
        RECORD_FILE="$2"; shift 2 ;;
      --record-file=*) RECORD_FILE="${1#*=}"; shift ;;
      --restart-mode)
        [ $# -ge 2 ] || { err "--restart-mode missing value"; exit 2; }
        RESTART_MODE="$2"; shift 2 ;;
      --restart-mode=*) RESTART_MODE="${1#*=}"; shift ;;
      --phase)
        [ $# -ge 2 ] || { err "--phase missing value"; exit 2; }
        PHASE="$2"; shift 2 ;;
      --phase=*)    PHASE="${1#*=}"; shift ;;
      --job-id)
        [ $# -ge 2 ] || { err "--job-id missing value"; exit 2; }
        JOB_ID="$2"; shift 2 ;;
      --job-id=*)   JOB_ID="${1#*=}"; shift ;;
      --state-file)
        [ $# -ge 2 ] || { err "--state-file missing value"; exit 2; }
        STATE_FILE="$2"; shift 2 ;;
      --state-file=*) STATE_FILE="${1#*=}"; shift ;;
      --pending-file)
        [ $# -ge 2 ] || { err "--pending-file missing value"; exit 2; }
        PENDING_FILE="$2"; shift 2 ;;
      --pending-file=*) PENDING_FILE="${1#*=}"; shift ;;
      --force-rollback) FORCE_ROLLBACK=1; shift ;;
      --timeout)
        [ $# -ge 2 ] || { err "--timeout missing value"; exit 2; }
        TIMEOUT="$2"; shift 2 ;;
      --timeout=*)    TIMEOUT="${1#*=}"; shift ;;
      --settle)
        [ $# -ge 2 ] || { err "--settle missing value"; exit 2; }
        SETTLE="$2"; shift 2 ;;
      --settle=*)     SETTLE="${1#*=}"; shift ;;
      --dry-run)      DRY_RUN=1; shift ;;
      --selftest)     SELFTEST=1; shift ;;
      -h|--help)      usage; exit 0 ;;
      *)              err "unknown argument: $1"; usage; exit 2 ;;
    esac
  done
}

main() {
  parse_args "$@"

  # selftest first: fully stubbed, no real defaults, no --target required
  if [ "$SELFTEST" -eq 1 ]; then
    run_selftest
    exit $?
  fi

  # ---------- phase validation ----------
  case "$PHASE" in
    full|pre-restart|boot-check|post-start) ;;
    *) err "invalid --phase '$PHASE' (expected: full | pre-restart | boot-check | post-start)"; exit 2 ;;
  esac
  if [ -n "$CONTAINER" ] && [ "$PHASE" != "full" ]; then
    err "--container (docker backend) only supports --phase full; two-phase runs in-container"
    exit 2
  fi

  # ---------- backend mode ----------
  if [ -n "$CONTAINER" ]; then
    BACKEND="docker"
    command -v docker >/dev/null 2>&1 \
      || { err "docker backend requires the docker CLI on the host"; exit 2; }
  fi

  # boot-check and post-start take their target from pending.json; the other
  # phases need it
  if [ "$PHASE" != "boot-check" ] && [ "$PHASE" != "post-start" ]; then
    [ -n "$TARGET" ] || { err "--target is required"; usage; exit 2; }
  fi
  if [ "$PHASE" = "pre-restart" ] && [ -z "$JOB_ID" ]; then
    err "pre-restart phase requires --job-id (plus --state-file/--pending-file from the plugin)"
    exit 2
  fi
  case "$TIMEOUT" in
    ''|*[!0-9]*) err "--timeout must be a positive integer (got '$TIMEOUT')"; exit 2 ;;
  esac
  [ "$TIMEOUT" -ge 1 ] || { err "--timeout must be >= 1"; exit 2; }

  # two-phase file defaults (the full phase ignores both)
  [ -n "$PENDING_FILE" ] || PENDING_FILE="${QWENPAW_WORKING_DIR:-.}/upgrade/pending.json"
  if [ -z "$STATE_FILE" ] && [ -n "$JOB_ID" ] && [ "$PHASE" != "full" ]; then
    STATE_FILE="${QWENPAW_WORKING_DIR:-.}/upgrade/upgrade-$JOB_ID.json"
  fi

  resolve_defaults

  # ---------- dry-run: print only, do not execute ----------
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$PHASE" != "full" ]; then
      err "--dry-run is only supported for the full phase"
      exit 2
    fi
    log "dry-run: printing the resolved commands only (no install/restart/health gate)"
    if [ "$BACKEND" = "docker" ]; then
      log "  backend : docker (container=$CONTAINER)"
    else
      log "  backend : in-container (restart-mode=$RESTART_MODE)"
    fi
    if [ -n "$CHECKER" ] && [ "$SKIP_CHECK" -eq 0 ]; then
      log "  0) check   : $(sub_target "$UG_CHECKER_CMD" "$TARGET")"
    fi
    if [ -n "$UG_PRE_INSTALL_CMD" ]; then
      log "  1a) ensure : $UG_PRE_INSTALL_CMD"
    fi
    log "  1) install : $(sub_target "$UG_INSTALL_CMD" "$TARGET")"
    log "  2) restart : $UG_RESTART_CMD"
    log "  3) health  : $UG_VERSION_CMD (poll until output == '$TARGET', timeout ${TIMEOUT}s; settle ${SETTLE}s)"
    if [ -n "$RECORD_FILE" ]; then
      log "  4) record  : $RECORD_FILE (written only after the health gate is all green)"
    fi
    exit 0
  fi

  case "$PHASE" in
    full)        do_upgrade ;;
    pre-restart) do_pre_restart ;;
    boot-check)  do_boot_check ;;
    post-start)  do_post_start ;;
  esac
}

# Default resolution (env hooks win); shared by main and selftest
resolve_defaults() {
  if [ -z "$WHEELHOUSE_DIR" ]; then
    WHEELHOUSE_DIR="${UG_WHEELHOUSE:-${QWENPAW_WORKING_DIR:-.}/upgrade/wheelhouse}"
  fi
  case "$RESTART_MODE" in
    supervisorctl) DEFAULT_RESTART="supervisorctl restart app" ;;
    # The worker is two processes: the qwenpaw-worker wrapper (PID 1) blocks on
    # the qwenpaw app child (the 8088 server). Killing the app makes the
    # wrapper's wait() return and the wrapper exits, so the container stops and
    # the runtime (restart policy / K8s) restarts it -> entrypoint runs the
    # boot-check (phase 2). The slim image has no ps/pkill, so scan /proc. The
    # cmdline is NUL-separated, so "qwenpaw" and "app" are separate fields —
    # convert NULs to spaces first, then match the server's distinctive
    # "qwenpaw app --host" (the wrapper's cmdline is "qwenpaw-worker", no
    # "app --host"). Exclude $$ so this shell (which carries the pattern in its
    # own cmdline) is not killed.
    kill)          DEFAULT_RESTART='for d in /proc/[0-9]*; do [ -r "$d/cmdline" ] || continue; p=$(basename "$d"); [ "$p" = "$$" ] && continue; c=$(tr "\0" " " < "$d/cmdline" 2>/dev/null); case "$c" in *"qwenpaw app --host"*) kill -9 "$p" 2>/dev/null; break;; esac; done' ;;
    *)             err "invalid --restart-mode: '$RESTART_MODE' (expected supervisorctl | kill)"; exit 2 ;;
  esac

  # venv resolution: UG_VENV hook > --venv > backend default
  if [ -z "$UG_VENV" ]; then
    if [ -n "$VENV" ]; then
      UG_VENV="$VENV"
    elif [ "$BACKEND" = "docker" ]; then
      UG_VENV="/opt/venv/qwenpaw"
    else
      UG_VENV="/app/venv"
    fi
  fi
  # Install tool: the runtime image may ship uv (fast path, surgical
  # --reinstall-package) or only pip (AgentTeams worker images, v1.2.x).
  # Explicit UG_UV/--uv forces the uv form; otherwise the install command
  # auto-detects at execution time and falls back to the venv pip with the
  # equivalent surgical flags (--force-reinstall --no-deps).

  if [ "$BACKEND" = "docker" ]; then
    # Host-side run: version probe and swap execute inside the container; restart = docker restart (writable layer kept)
    [ -n "$UG_VERSION_CMD" ] || UG_VERSION_CMD="docker exec $CONTAINER sh -c 'curl -fsS $VERSION_ENDPOINT || $UG_VENV/bin/qwenpaw --version'"
    if [ -n "$UG_UV" ]; then
      [ -n "$UG_INSTALL_CMD" ] || UG_INSTALL_CMD="docker exec $CONTAINER sh -c 'VIRTUAL_ENV=$UG_VENV $UG_UV pip install --reinstall-package qwenpaw qwenpaw=={target}'"
    else
      [ -n "$UG_INSTALL_CMD" ] || UG_INSTALL_CMD="docker exec $CONTAINER sh -c 'if command -v uv >/dev/null 2>&1; then VIRTUAL_ENV=$UG_VENV uv pip install --reinstall-package qwenpaw qwenpaw=={target}; else $UG_VENV/bin/pip install --force-reinstall --no-deps qwenpaw=={target}; fi'"
    fi
    [ -n "$UG_PATCH_CMD" ] || UG_PATCH_CMD="docker exec $CONTAINER sh -c '$UG_VENV/bin/python /opt/agentteams/scripts/patch-qwenpaw-driver-policy-reload.py'"
    [ -n "$UG_RESTART_CMD" ] || UG_RESTART_CMD="docker restart $CONTAINER"
    [ -n "$UG_ALIVE_CMD" ] || UG_ALIVE_CMD="docker inspect -f '{{.State.Running}}' $CONTAINER"
    # The rollback's `docker exec ... pip install` needs a running container;
    # after a wrapper crash the container is stopped, so start it first.
    [ -n "$UG_PRE_INSTALL_CMD" ] || UG_PRE_INSTALL_CMD='if [ "$(docker inspect -f "{{.State.Running}}" '"$CONTAINER"' 2>/dev/null)" != "true" ]; then echo "[upgrade] pre-install: container not running, starting it"; docker start '"$CONTAINER"'; fi'
  else
    if [ "$PHASE" = "boot-check" ]; then
      # The app is not up yet at boot-check time (this runs before
      # `exec qwenpaw-worker`): read the version from the venv metadata
      # directly instead of probing the live API.
      [ -n "$UG_VERSION_CMD" ] || UG_VERSION_CMD="$UG_VENV/bin/python -c 'import importlib.metadata as m; print(m.version(\"qwenpaw\"))'"
    else
      [ -n "$UG_VERSION_CMD" ] || UG_VERSION_CMD="curl -fsS $VERSION_ENDPOINT || $UG_VENV/bin/qwenpaw --version"
    fi
    if [ -n "$UG_UV" ]; then
      [ -n "$UG_INSTALL_CMD" ] || UG_INSTALL_CMD="VIRTUAL_ENV=$UG_VENV $UG_UV pip install --reinstall-package qwenpaw qwenpaw=={target}"
    else
      [ -n "$UG_INSTALL_CMD" ] || UG_INSTALL_CMD="sh -c 'if command -v uv >/dev/null 2>&1; then VIRTUAL_ENV=$UG_VENV uv pip install --reinstall-package qwenpaw qwenpaw=={target}; else $UG_VENV/bin/pip install --force-reinstall --no-deps qwenpaw=={target}; fi'"
    fi
    local _pd
    _pd="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -n "$UG_PATCH_CMD" ] || UG_PATCH_CMD="$UG_VENV/bin/python $_pd/patch-qwenpaw-driver-policy-reload.py"
    [ -n "$UG_RESTART_CMD" ] || UG_RESTART_CMD="$DEFAULT_RESTART"
  fi

  # S0: without an explicit --checker, fall back to the co-located pre-flight checker (merged via AgentTeams#1343)
  if [ -z "$CHECKER" ]; then
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$dir/check_qwenpaw_upgrade.py" ]; then
      CHECKER="$dir/check_qwenpaw_upgrade.py"
    else
      log "WARNING: default checker check_qwenpaw_upgrade.py not found; S0 pre-flight skipped"
    fi
  fi
  if [ -n "$CHECKER" ] && [ "$SKIP_CHECK" -eq 0 ] && [ -z "$UG_CHECKER_CMD" ]; then
    if [ "$PHASE" = "pre-restart" ]; then
      # venv python (the system python has no qwenpaw_worker) and the
      # wheelhouse: C3 is MANDATORY in pre-restart (a missing wheel is a FAIL,
      # and the app cannot download after the restart).
      UG_CHECKER_CMD="$UG_VENV/bin/python $CHECKER --target {target} --in-place --wheelhouse $WHEELHOUSE_DIR"
    else
      UG_CHECKER_CMD="python3 $CHECKER --target {target} --in-place"
    fi
  fi
}

main "$@"
