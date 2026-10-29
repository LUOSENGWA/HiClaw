#!/bin/bash
# check-coding-cli-run-governance.sh
#
# Tests run-coding-cli.sh's governance passthrough, its "ungoverned"
# visibility warning, and its success/failure/timeout outcome detection,
# using a STUB qwen on PATH — no real runner required, so this runs in CI.
# Closes the gap where the delegation executor (the script that actually runs
# the CLI) had no automated test (only detect + verify did).
#
# Cases:
#   1. flag passthrough (jq-independent): --allowed-tools reaches the runner
#      argv; with a budget/sandbox config present (needs jq) those flags do too.
#   2. ungoverned  — no config, no tool allowlist: the run log carries the
#      UNGOVERNED warning (code-level visibility), exit still 0.
#   3. tool-only   — only --allowed-tools (no budget/sandbox): still governed,
#      so NO warning (a tool allowlist is a real boundary).
#   4. timeout     — the runner outlives --timeout: exit 124 + TIMEOUT message
#      (the timeout-detection half of success/failure/timeout verification).
#   5. failure     — the runner exits non-zero: the code is propagated and NO
#      TIMEOUT is reported (the failure-detection half).
#   6. json-file   — --json-file reaches the runner argv and the runner's
#      structured-output file lands on disk (the stub writes it there), i.e.
#      the optional structured-output passthrough is wired end to end.
#   7. cli-missing — a wiped install fails fast (exit 125) with the
#      re-install hint.
#   8. distinct-home — a config in the delegating agent's ORIGINAL home
#      (HOME moved to QWENPAW_WORKING_DIR, no CODING_CLI_CONFIG): all four
#      controls reach the runner argv and --yolo is absent (#1409
#      regression: the persisted-home move must not hide the control
#      config).
#   9. new-home fallback — a config only in the effective (persisted) home
#      is still read (backward compat for configs created after the move).
#  10. both homes — different configs in both homes: the original home
#      wins.
#
# jq is optional: the config-driven budget/sandbox assertions are skipped (with
# a note) when jq is absent, matching the detect suite's "skip when a
# dependency is missing" convention.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_SCRIPT="${REPO_ROOT}/manager/agent/skills/coding-cli-management/scripts/run-coding-cli.sh"
if [ ! -f "${RUN_SCRIPT}" ]; then
    echo "FAIL setup (run-coding-cli.sh not found: ${RUN_SCRIPT})"
    exit 1
fi

HAS_JQ=0
command -v jq >/dev/null 2>&1 && HAS_JQ=1

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# Stub qwen: record its argv (one token per line) to $QWEN_STUB_ARGS. Behavior
# is selected by QWEN_STUB_MODE — default (exit 0) / sleep (sleep 5, to trip
# the timeout) / fail (exit 7). No real runner required, so this runs in CI.
STUB_BIN="${TMP}/bin"
STUB_ARGS="${TMP}/qwen-args.txt"
mkdir -p "${STUB_BIN}"
cat > "${STUB_BIN}/qwen" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" > "${QWEN_STUB_ARGS}"
# Emulate `qwen --json-file <path>`: write a minimal structured result to the
# given path so the wrapper's passthrough can be asserted end to end. A no-op
# for the other cases (none of them pass --json-file).
prev=""
for a in "$@"; do
    if [ "${prev}" = "--json-file" ]; then
        printf '{"type":"result","subtype":"success","result":"stub"}\n' > "${a}"
    fi
    prev="${a}"
done
case "${QWEN_STUB_MODE:-default}" in
    sleep) sleep 5; exit 0 ;;
    fail)  exit 7 ;;
    *)     exit 0 ;;
esac
STUB
chmod +x "${STUB_BIN}/qwen"

workspace="${TMP}/ws"
prompt="${TMP}/prompt.txt"
mkdir -p "${workspace}"
echo "do the thing" > "${prompt}"

fail=0
have_arg() { grep -qxF -- "$1" "${STUB_ARGS}" 2>/dev/null; }
argv_show() { tr '\n' ' ' < "${STUB_ARGS}" 2>/dev/null; }

run_qwen() {
    # $@ = extra run-coding-cli.sh flags; $CFG (may be empty) selects the config;
    # $QWEN_STUB_MODE (may be empty) selects the stub behavior.
    PATH="${STUB_BIN}:${PATH}" \
    QWEN_STUB_ARGS="${STUB_ARGS}" \
    CODING_CLI_CONFIG="${CFG:-${TMP}/nonexistent.json}" \
    bash "${RUN_SCRIPT}" --cli qwen --workspace "${workspace}" --prompt-file "${prompt}" "$@" 2>&1
}

run_qwen_distinct_home() {
    # $1 = the delegating agent's original home, $2 = the persisted working
    # dir (QWENPAW_WORKING_DIR), $3... = extra run-coding-cli.sh flags.
    # Unlike run_qwen this does NOT set CODING_CLI_CONFIG (it is pinned to
    # empty, which the wrapper treats as unset): it exercises the DEFAULT
    # config resolution with a moved HOME — the #1409 scenario.
    PATH="${STUB_BIN}:${PATH}" \
    QWEN_STUB_ARGS="${STUB_ARGS}" \
    CODING_CLI_CONFIG="" \
    HOME="$1" \
    QWENPAW_WORKING_DIR="$2" \
    bash "${RUN_SCRIPT}" --cli qwen --workspace "${workspace}" --prompt-file "${prompt}" "${@:3}" 2>&1
}

# --- case 1: flag passthrough always; config-driven budget/sandbox iff jq -----
cfg1="${TMP}/cfg1.json"
if [ "${HAS_JQ}" = "1" ]; then
    printf '{"max_session_turns": 5, "sandbox": true}' > "${cfg1}"
fi
CFG="${cfg1}" rm -f "${STUB_ARGS}"
out="$(CFG="${cfg1}" run_qwen --allowed-tools "Read,Grep")"
for want in --yolo --allowed-tools "Read,Grep"; do
    if ! have_arg "${want}"; then
        echo "FAIL case1: runner argv missing '${want}' (got: $(argv_show))"
        fail=1
    fi
done
if [ "${HAS_JQ}" = "1" ]; then
    for want in --max-session-turns 5 --sandbox; do
        if ! have_arg "${want}"; then
            echo "FAIL case1-config: runner argv missing '${want}' (got: $(argv_show))"
            fail=1
        fi
    done
else
    echo "SKIP case1 config-driven budget/sandbox assertions (jq absent)"
fi
if grep -q "UNGOVERNED" <<<"${out}"; then
    echo "FAIL case1: unexpected UNGOVERNED warning (got: ${out})"
    fail=1
fi

# --- case 2: ungoverned (no config, no tool allowlist) -> warning, exit 0 -----
CFG="" rm -f "${STUB_ARGS}"
out="$(CFG="" run_qwen)"
rc=$?
if [ "${rc}" -ne 0 ]; then
    echo "FAIL case2: run should still exit 0 (got ${rc}): ${out}"
    fail=1
fi
if ! grep -q "UNGOVERNED" <<<"${out}"; then
    echo "FAIL case2: expected UNGOVERNED warning, got: ${out}"
    fail=1
fi

# --- case 3: tool allowlist only -> governed, so NO warning -------------------
CFG="" rm -f "${STUB_ARGS}"
out="$(CFG="" run_qwen --allowed-tools "Read")"
if grep -q "UNGOVERNED" <<<"${out}"; then
    echo "FAIL case3: a tool allowlist is a real boundary; no UNGOVERNED warning expected (got: ${out})"
    fail=1
fi
if ! have_arg "--allowed-tools" || ! have_arg "Read"; then
    echo "FAIL case3: --allowed-tools Read should reach the runner argv (got: $(argv_show))"
    fail=1
fi

# --- case 4: timeout — runner outlives --timeout -> exit 124 + TIMEOUT --------
rm -f "${STUB_ARGS}"
out="$(QWEN_STUB_MODE=sleep run_qwen --timeout 1)"
rc=$?
if [ "${rc}" -ne 124 ]; then
    echo "FAIL case4: expected exit 124 on timeout (got ${rc}): ${out}"
    fail=1
fi
if ! grep -q "TIMEOUT" <<<"${out}"; then
    echo "FAIL case4: expected TIMEOUT message, got: ${out}"
    fail=1
fi

# --- case 5: failure — runner exits non-zero -> propagate code, no TIMEOUT ----
rm -f "${STUB_ARGS}"
out="$(QWEN_STUB_MODE=fail run_qwen)"
rc=$?
if [ "${rc}" -ne 7 ]; then
    echo "FAIL case5: expected exit 7 (the runner's code) (got ${rc}): ${out}"
    fail=1
fi
if grep -q "TIMEOUT" <<<"${out}"; then
    echo "FAIL case5: no TIMEOUT expected for a plain failure (got: ${out})"
    fail=1
fi
if ! grep -q "exit code 7" <<<"${out}"; then
    echo "FAIL case5: expected 'Finished with exit code 7' (got: ${out})"
    fail=1
fi

# --- case 6: --json-file passthrough — the flag reaches the runner argv and ---
#             the runner's structured-output file lands on disk --------------
json_out="${TMP}/json-out/result.json"
mkdir -p "$(dirname "${json_out}")"
rm -f "${STUB_ARGS}" "${json_out}"
out="$(CFG="" run_qwen --json-file "${json_out}")"
if ! have_arg "--json-file" || ! have_arg "${json_out}"; then
    echo "FAIL case6: --json-file should reach the runner argv (got: $(argv_show))"
    fail=1
fi
if [ ! -f "${json_out}" ]; then
    echo "FAIL case6: runner did not write the --json-file output at ${json_out}"
    fail=1
fi

# --- case 7: CLI missing (recreation wiped the install) -> actionable error --
# The recreation-wipe scenario: the CLI is configured (config says enabled) but
# its npm -g install was wiped when the worker container was recreated, while
# the auth config in the agent home survived. The pre-flight guard must fail
# fast with an actionable re-install message (exit 125), not a confusing
# mid-run "command not found". Run with a clean PATH that lacks the qwen stub
# to simulate the wiped install.
out="$(PATH="/usr/bin:/bin" \
    QWEN_STUB_ARGS="${STUB_ARGS}" \
    CODING_CLI_CONFIG="${TMP}/nonexistent.json" \
    bash "${RUN_SCRIPT}" --cli qwen --workspace "${workspace}" --prompt-file "${prompt}" 2>&1)"
rc=$?
if [ "${rc}" -ne 125 ]; then
    echo "FAIL case7: expected exit 125 when the CLI binary is missing (got ${rc}): ${out}"
    fail=1
fi
if ! grep -q "not on PATH" <<<"${out}"; then
    echo "FAIL case7: expected an actionable 'not on PATH' message (got: ${out})"
    fail=1
fi
if ! grep -qi "install endpoint\|recreat" <<<"${out}"; then
    echo "FAIL case7: expected an actionable re-install hint (got: ${out})"
    fail=1
fi

# --- cases 8-10: default config path with a moved HOME (#1409) --------------
# These do NOT set CODING_CLI_CONFIG: they exercise the default resolution
# with HOME (the delegating agent's original home) distinct from
# QWENPAW_WORKING_DIR (the persisted runtime home the wrapper moves HOME to).
# All three assert config-driven flags, so they need jq and follow the
# suite's skip-when-absent convention.
if [ "${HAS_JQ}" = "1" ]; then
    # --- case 8: distinct-home default config — original home is read ------
    orig_home="${TMP}/homes/orig8"
    work_home="${TMP}/homes/work8"
    mkdir -p "${orig_home}" "${work_home}"
    printf '{"approval_mode":"plan","max_session_turns":3,"max_wall_time":"30s","sandbox":true}' > "${orig_home}/coding-cli-config.json"
    rm -f "${STUB_ARGS}"
    out="$(run_qwen_distinct_home "${orig_home}" "${work_home}")"
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        echo "FAIL case8: distinct-home run should exit 0 (got ${rc}): ${out}"
        fail=1
    fi
    for want in --approval-mode plan --max-session-turns 3 --max-wall-time 30s --sandbox; do
        if ! have_arg "${want}"; then
            echo "FAIL case8: runner argv missing '${want}' (got: $(argv_show))"
            fail=1
        fi
    done
    if have_arg "--yolo"; then
        echo "FAIL case8: approval_mode plan from the original home must not add --yolo (got: $(argv_show))"
        fail=1
    fi

    # --- case 9: new-home fallback — config only in the effective home -----
    orig_home="${TMP}/homes/orig9"
    work_home="${TMP}/homes/work9"
    mkdir -p "${orig_home}" "${work_home}"
    printf '{"approval_mode":"plan","max_session_turns":3,"max_wall_time":"30s","sandbox":true}' > "${work_home}/coding-cli-config.json"
    rm -f "${STUB_ARGS}"
    out="$(run_qwen_distinct_home "${orig_home}" "${work_home}")"
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        echo "FAIL case9: new-home fallback run should exit 0 (got ${rc}): ${out}"
        fail=1
    fi
    for want in --approval-mode plan --max-session-turns 3 --max-wall-time 30s --sandbox; do
        if ! have_arg "${want}"; then
            echo "FAIL case9: effective-home config must still be read (got: $(argv_show))"
            fail=1
        fi
    done

    # --- case 10: both homes set — the original home's config wins ---------
    orig_home="${TMP}/homes/orig10"
    work_home="${TMP}/homes/work10"
    mkdir -p "${orig_home}" "${work_home}"
    printf '{"approval_mode":"plan","max_session_turns":3,"max_wall_time":"30s","sandbox":true}' > "${orig_home}/coding-cli-config.json"
    printf '{"approval_mode":"yolo","max_session_turns":9,"max_wall_time":"1h","sandbox":false}' > "${work_home}/coding-cli-config.json"
    rm -f "${STUB_ARGS}"
    out="$(run_qwen_distinct_home "${orig_home}" "${work_home}")"
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        echo "FAIL case10: both-homes run should exit 0 (got ${rc}): ${out}"
        fail=1
    fi
    if ! have_arg "--approval-mode" || ! have_arg "plan"; then
        echo "FAIL case10: the original home's config must win (got: $(argv_show))"
        fail=1
    fi
    if have_arg "--yolo" || have_arg "9" || have_arg "1h"; then
        echo "FAIL case10: the effective home's config must not apply (got: $(argv_show))"
        fail=1
    fi
else
    echo "SKIP cases 8-10 distinct-home default config (jq absent)"
fi

if [ "${fail}" -eq 0 ]; then
    echo "PASS coding-cli run governance (jq=${HAS_JQ})"
    exit 0
fi
echo "FAIL coding-cli run governance (jq=${HAS_JQ})"
exit 1
