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
# Steps:
#   S0  pre-flight   external checker (default: the co-located
#                    check_qwenpaw_upgrade.py); nonzero exit aborts before
#                    anything is installed (exit 2)
#   S3  package swap `uv pip install --reinstall-package qwenpaw` into the
#                    runtime venv (~3s cross-version; full-tree reinstalls
#                    deliberately avoided)
#   S4  restart      backend-specific primitive (see backends below); a
#                    failed restart is not rolled back immediately — the
#                    health gate below is the arbiter
#   S5  health gate  poll until the reported version equals the target
#                    (timeout Ns); timeout/mismatch triggers rollback
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
#
# Usage:
#   upgrade_qwenpaw_runtime.sh --target X.Y.Z [--checker PATH] [--skip-check]
#       [--container NAME] [--venv PATH] [--restart-mode supervisorctl|kill]
#       [--timeout 90] [--record-file PATH] [--dry-run] [--selftest]
#
# Environment hooks (all overridable; --selftest stubs every external action):
#   UG_VERSION_CMD   print the current version (raw "X.Y.Z" or the JSON
#                    {"version":"X"} form returned by /api/version; both are
#                    handled)
#   UG_INSTALL_CMD   install command with a {target} placeholder
#   UG_RESTART_CMD   restart command
#   UG_CHECKER_CMD   checker command with a {target} placeholder
#
# Red line: --selftest runs a fully stubbed battery and never touches a real
# deployment; --dry-run prints the resolved commands without executing them.
# ============================================================================
set -u

VERSION_ENDPOINT="http://127.0.0.1:8088/api/version"

# ---------- environment hooks (caller values are kept; real defaults resolved in resolve_defaults) ----------
UG_VERSION_CMD="${UG_VERSION_CMD:-}"
UG_INSTALL_CMD="${UG_INSTALL_CMD:-}"
UG_RESTART_CMD="${UG_RESTART_CMD:-}"
UG_CHECKER_CMD="${UG_CHECKER_CMD:-}"
UG_VENV="${UG_VENV:-}"
UG_UV="${UG_UV:-}"

# ---------- CLI state ----------
TARGET=""
CHECKER=""
SKIP_CHECK=0
CONTAINER=""
VENV=""
RESTART_MODE="supervisorctl"
TIMEOUT=90
RECORD_FILE=""
DRY_RUN=0
SELFTEST=0
BACKEND="in-container"

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

run_install() {
  local t="$1" cmd
  cmd="$(sub_target "$UG_INSTALL_CMD" "$t")"
  log "install: $cmd"
  bash -c "$cmd"
}

run_restart() {
  log "restart: $UG_RESTART_CMD"
  bash -c "$UG_RESTART_CMD"
}

# $1=expected version  $2=timeout seconds; poll until version == expected
health_gate() {
  local want="$1" limit="$2" now deadline v last=""
  now="$(date +%s)"
  deadline=$(( now + limit ))
  log "health gate: waiting for version == '$want' (timeout ${limit}s)"
  while :; do
    if v="$(get_version)"; then
      last="$v"
      if [ "$v" = "$want" ]; then
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
  if ! health_gate "$old" "$TIMEOUT"; then
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

  # 4) restart (failure does not roll back directly; the health gate below decides)
  if ! run_restart; then
    err "restart command failed (no immediate rollback; the health gate below decides)"
  fi

  # 5) health gate
  if ! health_gate "$TARGET" "$TIMEOUT"; then
    do_rollback "$old"
  fi

  # 7) success
  elapsed=$(( $(date +%s) - start_ts ))
  write_record "ok" "$TARGET"
  echo "SUMMARY: status=ok old=$old target=$TARGET elapsed=${elapsed}s"
  exit 0
}

# ============================================================================
# --selftest: built-in stubbed battery (7 scenarios; every external command mocked via tmp-dir state files)
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
  log "selftest: starting (7 scenarios, all external commands stubbed; tmp dir: $tmp)"

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
      case "$UG_CHECKER_CMD" in *"--target {target}") printf 'cok' ;; *) printf 'cbad' ;; esac
    } > "$d/asserts"
    exit 0
  ) > "$d/out.log" 2>&1
  rc=$?
  SC_ID="T6"
  st_begin T6 "docker backend default command assembly"
  st_check "exit" "0" "$rc"
  st_check "six assertions" "vokiokrokpokuokcok" "$(cat "$d/asserts" 2>/dev/null)"
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

  log "selftest: tmp dir kept at $tmp (not removed automatically)"
  if [ "$SELFTEST_TOTAL_FAIL" -eq 0 ]; then
    echo "SELFTEST: 7/7 PASS"
    return 0
  fi
  echo "SELFTEST: $SELFTEST_TOTAL_FAIL/7 scenarios FAIL"
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
  --restart-mode MODE   supervisorctl (in-container default) | kill
  --timeout N           health-gate timeout in seconds (default 90)
  --record-file PATH    write a JSON version record after a successful upgrade
                        (verified, then recorded)
  --dry-run             print the resolved commands only; no install/restart/health gate
  --selftest            run the built-in stubbed battery (7 scenarios, no real actions)
  -h, --help            this help

Environment hooks (all overridable; selftest relies on this mechanism):
  UG_VERSION_CMD / UG_INSTALL_CMD({target}) / UG_RESTART_CMD / UG_CHECKER_CMD({target})
  UG_VENV (default /app/venv, /opt/venv/qwenpaw with --container) / UG_UV (default /usr/bin/uv)

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
      --record-file)
        [ $# -ge 2 ] || { err "--record-file missing value"; exit 2; }
        RECORD_FILE="$2"; shift 2 ;;
      --record-file=*) RECORD_FILE="${1#*=}"; shift ;;
      --restart-mode)
        [ $# -ge 2 ] || { err "--restart-mode missing value"; exit 2; }
        RESTART_MODE="$2"; shift 2 ;;
      --restart-mode=*) RESTART_MODE="${1#*=}"; shift ;;
      --timeout)
        [ $# -ge 2 ] || { err "--timeout missing value"; exit 2; }
        TIMEOUT="$2"; shift 2 ;;
      --timeout=*)    TIMEOUT="${1#*=}"; shift ;;
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

  # ---------- backend mode ----------
  if [ -n "$CONTAINER" ]; then
    BACKEND="docker"
    command -v docker >/dev/null 2>&1 \
      || { err "docker backend requires the docker CLI on the host"; exit 2; }
  fi

  [ -n "$TARGET" ] || { err "--target is required"; usage; exit 2; }
  case "$TIMEOUT" in
    ''|*[!0-9]*) err "--timeout must be a positive integer (got '$TIMEOUT')"; exit 2 ;;
  esac
  [ "$TIMEOUT" -ge 1 ] || { err "--timeout must be >= 1"; exit 2; }

  resolve_defaults

  # ---------- dry-run: print only, do not execute ----------
  if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run: printing the resolved commands only (no install/restart/health gate)"
    if [ "$BACKEND" = "docker" ]; then
      log "  backend : docker (container=$CONTAINER)"
    else
      log "  backend : in-container (restart-mode=$RESTART_MODE)"
    fi
    if [ -n "$CHECKER" ] && [ "$SKIP_CHECK" -eq 0 ]; then
      log "  0) check   : $(sub_target "$UG_CHECKER_CMD" "$TARGET")"
    fi
    log "  1) install : $(sub_target "$UG_INSTALL_CMD" "$TARGET")"
    log "  2) restart : $UG_RESTART_CMD"
    log "  3) health  : $UG_VERSION_CMD (poll until output == '$TARGET', timeout ${TIMEOUT}s)"
    if [ -n "$RECORD_FILE" ]; then
      log "  4) record  : $RECORD_FILE (written only after the health gate is all green)"
    fi
    exit 0
  fi

  do_upgrade
}

# Default resolution (env hooks win); shared by main and selftest
resolve_defaults() {
  case "$RESTART_MODE" in
    supervisorctl) DEFAULT_RESTART="supervisorctl restart app" ;;
    kill)          DEFAULT_RESTART='pkill -9 -f "qwenpaw app"' ;;
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
  [ -n "$UG_UV" ] || UG_UV="/usr/bin/uv"

  if [ "$BACKEND" = "docker" ]; then
    # Host-side run: version probe and swap execute inside the container; restart = docker restart (writable layer kept)
    [ -n "$UG_VERSION_CMD" ] || UG_VERSION_CMD="docker exec $CONTAINER sh -c 'curl -fsS $VERSION_ENDPOINT || $UG_VENV/bin/qwenpaw --version'"
    [ -n "$UG_INSTALL_CMD" ] || UG_INSTALL_CMD="docker exec $CONTAINER sh -c 'VIRTUAL_ENV=$UG_VENV $UG_UV pip install --reinstall-package qwenpaw qwenpaw=={target}'"
    [ -n "$UG_RESTART_CMD" ] || UG_RESTART_CMD="docker restart $CONTAINER"
  else
    [ -n "$UG_VERSION_CMD" ] || UG_VERSION_CMD="curl -fsS $VERSION_ENDPOINT || $UG_VENV/bin/qwenpaw --version"
    [ -n "$UG_INSTALL_CMD" ] || UG_INSTALL_CMD="VIRTUAL_ENV=$UG_VENV $UG_UV pip install --reinstall-package qwenpaw qwenpaw=={target}"
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
    UG_CHECKER_CMD="python3 $CHECKER --target {target}"
  fi
}

main "$@"
