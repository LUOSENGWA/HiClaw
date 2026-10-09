#!/bin/bash
# check-coding-cli-run-governance.sh
#
# Tests run-coding-cli.sh's governance passthrough and its "ungoverned"
# visibility warning, using a STUB qwen on PATH — no real runner required, so
# this runs in CI. Closes the gap where the delegation executor (the script
# that actually runs the CLI) had no automated test (only detect + verify did).
#
# Cases:
#   1. flag passthrough (jq-independent): --allowed-tools reaches the runner
#      argv; with a budget/sandbox config present (needs jq) those flags do too.
#   2. ungoverned  — no config, no tool allowlist: the run log carries the
#      UNGOVERNED warning (code-level visibility), exit still 0.
#   3. tool-only   — only --allowed-tools (no budget/sandbox): still governed,
#      so NO warning (a tool allowlist is a real boundary).
#
# jq is optional: the config-driven budget/sandbox assertions are skipped (with
# a note) when jq is absent, matching the detect suite's "skip when a
# dependency is missing" convention.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_SCRIPT="${REPO_ROOT}/manager/agent/skills-alpha/coding-cli-management/scripts/run-coding-cli.sh"
if [ ! -f "${RUN_SCRIPT}" ]; then
    echo "FAIL setup (run-coding-cli.sh not found: ${RUN_SCRIPT})"
    exit 1
fi

HAS_JQ=0
command -v jq >/dev/null 2>&1 && HAS_JQ=1

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# Stub qwen: record its argv (one token per line) to $QWEN_STUB_ARGS, exit 0.
STUB_BIN="${TMP}/bin"
STUB_ARGS="${TMP}/qwen-args.txt"
mkdir -p "${STUB_BIN}"
cat > "${STUB_BIN}/qwen" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" > "${QWEN_STUB_ARGS}"
exit 0
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
    # $@ = extra run-coding-cli.sh flags; $CFG (may be empty) selects the config.
    PATH="${STUB_BIN}:${PATH}" \
    QWEN_STUB_ARGS="${STUB_ARGS}" \
    CODING_CLI_CONFIG="${CFG:-${TMP}/nonexistent.json}" \
    bash "${RUN_SCRIPT}" --cli qwen --workspace "${workspace}" --prompt-file "${prompt}" "$@" 2>&1
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

if [ "${fail}" -eq 0 ]; then
    echo "PASS coding-cli run governance (jq=${HAS_JQ})"
    exit 0
fi
echo "FAIL coding-cli run governance (jq=${HAS_JQ})"
exit 1
