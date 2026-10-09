#!/bin/bash
# Execute an AI coding CLI tool in a given workspace
# Usage: run-coding-cli.sh --cli <tool> --workspace <dir> --prompt-file <file> [--timeout <secs>]
#        [--model <name>] [--allowed-tools <csv>] [--allowed-mcp <csv>] [--safe-mode] [--bare]
# (governance flags apply to the qwen runner; other runners warn and ignore them)

set -e

cli=""
workspace=""
prompt_file=""
timeout_secs=600
g_model=""
g_allowed_tools=""
g_allowed_mcp=""
g_safe_mode=0
g_bare=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cli)         cli="$2";         shift 2 ;;
        --workspace)   workspace="$2";   shift 2 ;;
        --prompt-file) prompt_file="$2"; shift 2 ;;
        --timeout)     timeout_secs="$2"; shift 2 ;;
        --model)       g_model="$2";       shift 2 ;;
        --allowed-tools) g_allowed_tools="$2"; shift 2 ;;
        --allowed-mcp) g_allowed_mcp="$2"; shift 2 ;;
        --safe-mode)   g_safe_mode=1;      shift ;;
        --bare)        g_bare=1;           shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [ -z "$cli" ] || [ -z "$workspace" ] || [ -z "$prompt_file" ]; then
    echo "Usage: $0 --cli <tool> --workspace <dir> --prompt-file <file> [--timeout <secs>]" >&2
    exit 1
fi

if [ ! -f "$prompt_file" ]; then
    echo "Prompt file not found: $prompt_file" >&2
    exit 1
fi

if [ ! -d "$workspace" ]; then
    echo "Workspace directory not found: $workspace" >&2
    exit 1
fi

# Governance flags are defined by the qwen runner; other runners have no
# equivalent surface, so warn and ignore rather than fail the run.
if [ "$cli" != "qwen" ] && { [ -n "$g_model" ] || [ -n "$g_allowed_tools" ] || [ -n "$g_allowed_mcp" ] || [ "$g_safe_mode" = "1" ] || [ "$g_bare" = "1" ]; }; then
    echo "[run-coding-cli] note: governance flags (--model/--allowed-tools/--allowed-mcp/--safe-mode/--bare) are qwen-specific and were ignored for cli=$cli" >&2
fi

# Save output to log file in the workspace's coding-cli-logs directory
log_dir="$workspace/coding-cli-logs"
mkdir -p "$log_dir"
timestamp=$(date +%Y%m%d-%H%M%S)
log_file="$log_dir/run-${timestamp}.log"

# Write the run metadata header to the log (and stdout) so the log file itself
# records which workspace/prompt/timeout this run used.
echo "[run-coding-cli] cli=$cli workspace=$workspace prompt_file=$prompt_file timeout=${timeout_secs}s" | tee "$log_file"
echo "[run-coding-cli] Log: $log_file"

# Optional per-runner budget fields from the delegating agent's config
# (default ~/coding-cli-config.json, override with CODING_CLI_CONFIG for
# tests). Only the qwen case consumes them today; absent/0/false -> no
# extra flags.
config_file="${CODING_CLI_CONFIG:-${HOME}/coding-cli-config.json}"
max_session_turns=""
sandbox_setting=""
if command -v jq >/dev/null 2>&1 && [ -f "${config_file}" ]; then
    max_session_turns=$(jq -r '.max_session_turns // empty' "${config_file}" 2>/dev/null || true)
    sandbox_setting=$(jq -r '.sandbox // empty' "${config_file}" 2>/dev/null || true)
fi

cd "$workspace"

case "$cli" in
    claude)
        timeout "$timeout_secs" claude -p "$(cat "$prompt_file")" \
            --dangerously-skip-permissions --output-format text 2>&1 | tee -a "$log_file"
        exit_code=${PIPESTATUS[0]}
        ;;
    gemini)
        timeout "$timeout_secs" gemini -p "$(cat "$prompt_file")" -y 2>&1 | tee -a "$log_file"
        exit_code=${PIPESTATUS[0]}
        ;;
    qodercli)
        timeout "$timeout_secs" qodercli -p "$(cat "$prompt_file")" \
            --yolo -w "$workspace" 2>&1 | tee -a "$log_file"
        exit_code=${PIPESTATUS[0]}
        ;;
    qwen)
        # qwen has no workspace flag (verified through qwen 0.25.0); its workspace is the
        # process cwd, so pin the run to $workspace explicitly.
        cd "$workspace"
        # Optional budget flags from the config (see "Config File" in
        # SKILL.md): max_session_turns -> --max-session-turns N (absent/0
        # = omit); sandbox: true -> --sandbox (Docker-backed, requires
        # docker at the execution site). No other runner consumes these.
        qwen_args=(--yolo)
        if [ -n "${max_session_turns}" ] && [ "${max_session_turns}" != "0" ]; then
            qwen_args+=(--max-session-turns "${max_session_turns}")
        fi
        if [ "${sandbox_setting}" = "true" ]; then
            qwen_args+=(--sandbox)
        fi
        # Governance passthrough (delegating agent's per-run control surface):
        # --model <name>       per-run model override (two-tier model control)
        # --allowed-tools <csv> tool allowlist (code-level permission boundary)
        # --allowed-mcp <csv>   MCP server allowlist (scenario isolation)
        # --safe-mode / --bare  clean / minimal config loading
        if [ -n "${g_model}" ]; then
            qwen_args+=(-m "${g_model}")
        fi
        if [ -n "${g_allowed_tools}" ]; then
            qwen_args+=(--allowed-tools "${g_allowed_tools}")
        fi
        if [ -n "${g_allowed_mcp}" ]; then
            qwen_args+=(--allowed-mcp-server-names "${g_allowed_mcp}")
        fi
        if [ "${g_safe_mode}" = "1" ]; then
            qwen_args+=(--safe-mode)
        fi
        if [ "${g_bare}" = "1" ]; then
            qwen_args+=(--bare)
        fi
        # Governance visibility (code-level signal, NOT a hard gate): an
        # unattended qwen run that has none of a turn budget / sandbox / tool
        # allowlist is "ungoverned" — all tools, no container isolation, bounded
        # only by the wall-clock timeout above. Surface it in the run log (the
        # durable record) so the risk is visible even if the caller never set
        # the config. Tighten via ${config_file} (max_session_turns / sandbox)
        # or the per-run flags above. A bare run stays a deliberate option —
        # this only makes it visible, it never blocks it.
        ungoverned=1
        if [ -n "${max_session_turns}" ] && [ "${max_session_turns}" != "0" ]; then ungoverned=0; fi
        if [ "${sandbox_setting}" = "true" ]; then ungoverned=0; fi
        if [ -n "${g_allowed_tools}" ]; then ungoverned=0; fi
        if [ "$ungoverned" = "1" ]; then
            echo "[run-coding-cli] WARNING: unattended qwen run is UNGOVERNED (no turn budget, no --sandbox, no tool allowlist) — bounded only by the ${timeout_secs}s wall-clock timeout. Tighten via ${config_file} (max_session_turns/sandbox) or --allowed-tools." | tee -a "$log_file"
        fi
        timeout "$timeout_secs" qwen "${qwen_args[@]}" "$(cat "$prompt_file")" 2>&1 | tee -a "$log_file"
        exit_code=${PIPESTATUS[0]}
        ;;
    opencode)
        # `opencode run --dir <path>` is the native working-directory flag
        # (opencode.ai/docs/cli); pin the run to $workspace explicitly.
        cd "$workspace"
        timeout "$timeout_secs" opencode run --auto --dir "$workspace" "$(cat "$prompt_file")" 2>&1 | tee -a "$log_file"
        exit_code=${PIPESTATUS[0]}
        ;;
    *)
        echo "Unknown CLI tool: $cli. Supported: claude, gemini, qodercli, qwen, opencode" >&2 | tee -a "$log_file"
        exit 1
        ;;
esac

if [ "$exit_code" -eq 124 ]; then
    echo "[run-coding-cli] TIMEOUT after ${timeout_secs}s" | tee -a "$log_file"
    exit 124
fi

echo "[run-coding-cli] Finished with exit code $exit_code" | tee -a "$log_file"
exit "$exit_code"
