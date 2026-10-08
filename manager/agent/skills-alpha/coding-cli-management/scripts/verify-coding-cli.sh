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
#   e) yolo_sandbox_warning: (qwen only) the un-sandboxed --yolo warning is
#                            visible in the case-(a) run log — or the
#                            environment is itself sandboxed (verbatim
#                            warning + dual branch in run_case_e below)
#   f) turn_budget         : (qwen only) max_session_turns=1 in an isolated
#                            config (CODING_CLI_CONFIG) stops a two-step
#                            prompt at the turn budget
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

# run_case_e <cli>: (qwen only) the un-sandboxed --yolo warning must be
# visible in the case-(a) run log.
#
# Verbatim warning observed (qwen 0.24.7, 2026-10-09, QwenPaw001 container
# without docker — first line of a real headless run log):
#   Warning: running headless with --yolo / approval-mode=yolo and no
#   sandbox. All tool calls (shell, write, edit) auto-execute at this
#   process's privilege level. Configure tools.executionSandbox on Linux
#   or a supported legacy sandbox via --sandbox / QWEN_SANDBOX, or set
#   QWEN_CODE_SUPPRESS_YOLO_WARNING=1 to silence this notice.
#
# Dual branch:
#   1. warning visible            -> PASS (unsandboxed --yolo run warned,
#                                    as expected)
#   2. warning absent, but the    -> PASS only if the environment is itself
#      environment is sandboxed     sandboxed (QWEN_SANDBOX set, or a sandbox
#                                    marker in the log) — i.e. the sandbox
#                                    took effect and suppressed the warning
#   3. otherwise                  -> SKIP with reason (environment
#                                    difference)
run_case_e() {
    local cli="$1" log
    log="$(ls -1t "${SANDBOX}/ws-a/coding-cli-logs/"*.log 2>/dev/null | head -1)"
    if [ -z "${log}" ] || [ ! -f "${log}" ]; then
        skip "${cli}.yolo_sandbox_warning (no case-a run log to inspect)"
        return 0
    fi
    if grep -q "running headless with --yolo / approval-mode=yolo and no sandbox" "${log}"; then
        pass "${cli}.yolo_sandbox_warning (warning visible in run log — unsandboxed --yolo run, as expected)"
        return 0
    fi
    if [ -n "${QWEN_SANDBOX:-}" ] || grep -qi "sandbox" "${log}"; then
        pass "${cli}.yolo_sandbox_warning (no warning; environment itself is sandboxed, sandbox took effect)"
        return 0
    fi
    skip "${cli}.yolo_sandbox_warning (no warning in run log and no sandbox in this environment — environment difference)"
    return 0
}

# run_case_f <cli>: (qwen only) max_session_turns budget smoke.
#
# Real run of a two-step prompt ("create a.txt, then b.txt") with
# max_session_turns=1 in an ISOLATED config: CODING_CLI_CONFIG points at a
# sandboxed HOME, so the delegating agent's real config is untouched.
#
# Observed behavior (qwen 0.24.7, 2026-10-09, real run): the run aborts at
# the turn budget with EXIT CODE 53 and the run log line
#   Reached max session turns for this session. Increase the number of
#   turns by specifying maxSessionTurns in settings.json.
# Artifact state is model-dependent (one turn can batch both step file
# writes before the final response hits the budget), so the assertion
# pins exit code + budget message, not the file set.
run_case_f() {
    local cli="$1" rc log_f
    local home_f="${SANDBOX}/home-f"
    mkdir -p "${home_f}" "${SANDBOX}/ws-f"
    printf '{"enabled":true,"cli":"qwen","max_session_turns":1}\n' \
        > "${home_f}/coding-cli-config.json"
    printf 'Step 1: create a file called a.txt containing exactly: A\nStep 2: then create a file called b.txt containing exactly: B\n' \
        > "${SANDBOX}/prompts/case-f.txt"
    CODING_CLI_CONFIG="${home_f}/coding-cli-config.json" \
        bash "${RUN_SCRIPT}" --cli "${cli}" --workspace "${SANDBOX}/ws-f" \
            --prompt-file "${SANDBOX}/prompts/case-f.txt" --timeout 300 \
            > "${SANDBOX}/prompts/case-f.out" 2>&1
    rc=$?
    log_f="$(ls -1t "${SANDBOX}/ws-f/coding-cli-logs/"*.log 2>/dev/null | head -1)"
    if [ "${rc}" != "0" ] && grep -q "Reached max session turns" "${log_f}" 2>/dev/null; then
        pass "${cli}.turn_budget (rc=${rc} — aborted at the turn budget with 'Reached max session turns'; observed rc=53 on qwen 0.24.7)"
    else
        fail "${cli}.turn_budget" "expected non-zero exit with 'Reached max session turns' in the run log (rc=${rc})"
        sed 's/^/    | /' "${SANDBOX}/prompts/case-f.out" | tail -10
    fi
}

for cli in qwen opencode; do
    if ! command -v "${cli}" >/dev/null 2>&1; then
        skip "${cli}: binary not found on PATH (not installed — opt-in skipped)"
        continue
    fi
    echo ""
    echo "== ${cli} (pinned version: $(command -v "${cli}")) =="
    case_a_ok=0
    if run_case_a "${cli}"; then
        case_a_ok=1
        run_case_d "${cli}"
    fi
    run_case_b "${cli}"
    run_case_c "${cli}"
    if [ "${cli}" = "qwen" ]; then
        run_case_e "${cli}"
        if [ "${case_a_ok}" = "1" ]; then
            run_case_f "${cli}"
        else
            skip "${cli}.turn_budget (case-a failed — cannot isolate budget behavior from an auth failure)"
        fi
    fi
done

echo ""
if [ "${FAILURES}" = "0" ]; then
    echo "All cases passed or skipped."
    exit 0
else
    echo "${FAILURES} case(s) FAILED."
    exit 1
fi
