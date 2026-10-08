#!/bin/bash
# verify-coding-cli.sh — opt-in execution verification for the coding CLI
# runners of run-coding-cli.sh.
#
# Pinned runner versions (verified on the AgentTeams dev host, 2026-10-09):
#   qwen     0.24.7  (verified flags: --yolo, --approval-mode, --max-wall-time)
#   opencode not pinned yet — install to verify
#
# Opt-in by design: a CLI whose binary is not on PATH is reported as
# [SKIP] <cli>: <reason> and does NOT fail the suite. Run this where a real
# (or stub) runner is installed:
#
#   bash manager/agent/skills-alpha/coding-cli-management/scripts/verify-coding-cli.sh
#
# Cases per CLI:
#   a) headless_success    : fresh workspace + minimal prompt
#                            ("Create a file called hello.txt containing
#                            exactly: ok") -> run-coding-cli.sh exit 0 and
#                            workspace/hello.txt contains "ok"
#   b) exit_code_propagate : run under a clean HOME (no auth) ->
#                            run-coding-cli.sh exits non-zero (the CLI's
#                            non-zero code is passed through; the `tee` in
#                            the pipeline does not swallow it — the script
#                            reads PIPESTATUS[0] right after the pipeline)
#   c) timeout_kill        : stub runner that sleeps 30s + --timeout 5 ->
#                            killed by the outer `timeout`, exit non-zero
#                            (124, or whatever the runner passes through)
#   d) workspace_boundary  : the artifact from case (a) is inside the
#                            --workspace dir, and the run log records
#                            workspace=<dir>
#
# Note for (c) on qwen: qwen 0.24.7 also has a native run-level budget,
# `qwen --max-wall-time <secs>`, which aborts the run with exit code 55.
# The outer `timeout` in run-coding-cli.sh remains the hard backstop for
# all runners; the stub case above verifies that wrapper path without
# costing a real model run.
#
# Exit: 0 if every case passes or is skipped (with reason), 1 on any failure.

set -u

# Locate the repo root from this file's own path
# (manager/agent/skills-alpha/coding-cli-management/scripts/ -> 5 levels up).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../../../" && pwd)"

RUN_SCRIPT="${SCRIPT_DIR}/run-coding-cli.sh"
if [ ! -f "${RUN_SCRIPT}" ]; then
    echo "FAIL setup (run-coding-cli.sh not found: ${RUN_SCRIPT})"
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "[SKIP] verify-coding-cli (jq not found)"
    exit 0
fi

SANDBOX="$(mktemp -d)"
trap 'rm -rf "${SANDBOX}"' EXIT

mkdir -p "${SANDBOX}/bin" "${SANDBOX}/home-clean" "${SANDBOX}/ws-a" \
         "${SANDBOX}/ws-b" "${SANDBOX}/ws-c" "${SANDBOX}/prompts"

PROMPT_FILE="${SANDBOX}/prompts/case-a.txt"
printf 'Create a file called hello.txt containing exactly: ok' > "${PROMPT_FILE}"

FAILURES=0

pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1 ($2)"; FAILURES=$((FAILURES + 1)); }
skip() { echo "[SKIP] $1"; }

# run_case_a <cli>: real headless run in $SANDBOX/ws-a (also feeds case d).
run_case_a() {
    local cli="$1" rc
    bash "${RUN_SCRIPT}" --cli "${cli}" --workspace "${SANDBOX}/ws-a" \
        --prompt-file "${PROMPT_FILE}" --timeout 300 \
        > "${SANDBOX}/prompts/case-a.out" 2>&1
    rc=$?
    local problems=""
    [ "${rc}" = "0" ] || problems="exit=${rc}; "
    [ -f "${SANDBOX}/ws-a/hello.txt" ] || problems="hello.txt missing; "
    if [ -f "${SANDBOX}/ws-a/hello.txt" ]; then
        [ "$(cat "${SANDBOX}/ws-a/hello.txt")" = "ok" ] || problems="hello.txt content wrong; "
    fi
    if [ -n "${problems}" ]; then
        fail "${cli}.headless_success" "${problems}"
        sed 's/^/    | /' "${SANDBOX}/prompts/case-a.out" | tail -10
        return 1
    fi
    pass "${cli}.headless_success"
    return 0
}

# run_case_d <cli>: artifact location + run log workspace= line (uses case a).
run_case_d() {
    local cli="$1" log problems=""
    log="$(ls -1t "${SANDBOX}/ws-a/coding-cli-logs/"*.log 2>/dev/null | head -1)"
    [ -f "${SANDBOX}/ws-a/hello.txt" ] || problems="artifact not in workspace; "
    [ -n "${log}" ] || problems="no run log in workspace/coding-cli-logs; "
    if [ -n "${log}" ]; then
        grep -q "workspace=${SANDBOX}/ws-a" "${log}" \
            || problems="run log does not record workspace= path; "
    fi
    if [ -n "${problems}" ]; then
        fail "${cli}.workspace_boundary" "${problems}"
    else
        pass "${cli}.workspace_boundary"
    fi
}

# run_case_b <cli>: clean-HOME run must exit non-zero (auth failure path).
run_case_b() {
    local cli="$1" rc
    env -i HOME="${SANDBOX}/home-clean" PATH="${PATH}" \
        bash "${RUN_SCRIPT}" --cli "${cli}" --workspace "${SANDBOX}/ws-b" \
            --prompt-file "${PROMPT_FILE}" --timeout 120 \
            > "${SANDBOX}/prompts/case-b.out" 2>&1
    rc=$?
    if [ "${rc}" != "0" ]; then
        pass "${cli}.exit_code_propagate (rc=${rc})"
    else
        fail "${cli}.exit_code_propagate" "expected non-zero exit, got 0"
        sed 's/^/    | /' "${SANDBOX}/prompts/case-b.out" | tail -10
    fi
}

# run_case_c <cli>: stub runner that sleeps 30s must be killed at --timeout.
run_case_c() {
    local cli="$1" rc
    cat > "${SANDBOX}/bin/${cli}" <<'SLEEP_STUB'
#!/bin/bash
# Stub runner for verify-coding-cli.sh case (c): sleep past the timeout.
exec sleep 30
SLEEP_STUB
    chmod +x "${SANDBOX}/bin/${cli}"
    PATH="${SANDBOX}/bin:${PATH}" \
        bash "${RUN_SCRIPT}" --cli "${cli}" --workspace "${SANDBOX}/ws-c" \
            --prompt-file "${PROMPT_FILE}" --timeout 5 \
            > "${SANDBOX}/prompts/case-c.out" 2>&1
    rc=$?
    if [ "${rc}" != "0" ]; then
        pass "${cli}.timeout_kill (rc=${rc})"
    else
        fail "${cli}.timeout_kill" "expected non-zero exit after timeout, got 0"
    fi
}

for cli in qwen opencode; do
    if ! command -v "${cli}" >/dev/null 2>&1; then
        skip "${cli}: binary not found on PATH (not installed — opt-in skipped)"
        continue
    fi
    echo ""
    echo "== ${cli} (pinned version: $(command -v "${cli}")) =="
    if run_case_a "${cli}"; then
        run_case_d "${cli}"
    fi
    run_case_b "${cli}"
    run_case_c "${cli}"
done

echo ""
if [ "${FAILURES}" = "0" ]; then
    echo "All cases passed or skipped."
    exit 0
else
    echo "${FAILURES} case(s) FAILED."
    exit 1
fi
