---
name: coding-cli-management
description: "Execute AI coding CLI tools (Claude Code / Gemini CLI / qodercli / Qwen Code / OpenCode) on behalf of Workers. Use when a Worker sends a coding-request: message, asking the delegating agent (Manager, team Leader, or an authorized Worker) to run coding operations in their workspace."
---

# Coding CLI Management

This skill enables the delegating agent (Manager, team Leader, or an authorized Worker) to execute AI coding CLI tools (claude/gemini/qodercli/qwen/opencode) on behalf of Workers. Workers generate precise prompts; the delegating agent runs the CLI in the Worker's workspace and returns the result.

## Config File

Path: `~/coding-cli-config.json` — this is the **delegating agent's own** config file (the agent running this skill), not a shared one.

```json
{
  "enabled": true,
  "cli": "claude",
  "confirmed_at": "2026-02-23T10:00:00Z"
}
```

| `enabled` | `cli` | Meaning |
|-----------|-------|---------|
| `false`   | any   | Admin declined; use normal task flow |
| `true`    | `"claude"` / `"gemini"` / `"qodercli"` / `"qwen"` / `"opencode"` | Active — use this CLI |

**Runner notes (Qwen Code / OpenCode).** Headless invocations used by `run-coding-cli.sh`: `qwen --yolo "<prompt>"` and `opencode run --auto --dir <workspace> "<prompt>"`. Config surfaces: `~/.qwen` (`settings.json` `security.auth`, or provider env) and `~/.config/opencode` plus `~/.local/share/opencode/auth.json` (credentials stored by `opencode auth login`) or provider env. Both runners were verified with a headless round trip before inclusion.

**Unattended semantics — read before enabling.**
- `qwen --yolo` automatically approves **all** tool calls — file edits and shell commands included, with no further prompts.
- `opencode run --auto` auto-approves permissions, but **explicit deny rules are still enforced**.
- **Neither changing the working directory nor adding a timeout establishes a permission boundary.** The workspace and the timeout shape scope and duration only; the runner keeps every capability of the delegating agent's process.

---

## Execution boundary

The CLI process runs **as the delegating agent** (Manager, team Leader, or an authorized Worker). It therefore inherits that agent's filesystem visibility and its environment — including any credentials present as environment variables — and nothing in this skill changes that.

When deciding whether to enable CLI delegation, assess at least:

- **Workspace access scope** — the run is pinned to the task workspace, but a runner in auto-approve mode can reach anything the delegating agent's account can.
- **Other teams' files** — anything on the shared filesystem the delegating agent can read or write (e.g. other tasks' directories).
- **Delegating agent's credentials** — API keys and tokens in the agent's environment are visible to the CLI process.
- **Mounted container sockets** — a mounted `docker.sock` (or equivalent) gives the CLI effective host-control capability.
- **Admin-plane capability** — any orchestration/admin CLI or API the delegating agent can use is equally available to the runner.

Mitigations:

- The CLI's native permission controls: `qwen --approval-mode plan|default|auto-edit|auto|yolo` (lower tiers keep an approval gate), and `opencode` explicit deny rules (still enforced under `--auto`).
- An isolated execution environment for the CLI: a dedicated container, a dedicated user, or restricted mounts.

No new approval subsystem is introduced by this skill.

---

## Step 1: First-Time Detection (before assigning a coding task)

Run when `~/coding-cli-config.json` does not exist:

```bash
bash /opt/agentteams/agent/skills/coding-cli-management/scripts/detect-available-cli.sh
```

**If no CLIs are available** (`available` array is empty):
```bash
echo '{"enabled":false,"cli":null,"confirmed_at":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' \
  > ~/coding-cli-config.json
```
Proceed with normal task assignment (Worker codes on their own).

**If CLIs are available**, ask the admin via the primary channel or Matrix DM — **in the language the admin used**:
> I found the following AI coding CLI tools available: [list]. Would you like to enable CLI delegation mode? Workers will generate coding prompts, and I'll use the CLI tool to make the code changes. Note: the CLI runs unattended — `qwen --yolo` auto-approves all tool calls, and `opencode --auto` still enforces explicit deny rules; the workspace and the timeout are not permission boundaries. Reply with the tool name (claude/gemini/qodercli/qwen/opencode) to enable, or 'no' to have workers code on their own.

On admin reply:
- Tool name (`claude` / `gemini` / `qodercli` / `qwen` / `opencode`):
  ```bash
  echo '{"enabled":true,"cli":"<chosen-tool>","confirmed_at":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' \
    > ~/coding-cli-config.json
  ```
- `"no"` or decline:
  ```bash
  echo '{"enabled":false,"cli":null,"confirmed_at":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' \
    > ~/coding-cli-config.json
  ```

---

## Step 2: Assigning a Coding Task (CLI mode enabled)

When `coding-cli-config.json` has `enabled: true`:

1. **Ensure the Worker has the `coding-cli` skill.** Query its Worker CR:
   ```bash
   agt get workers <worker> -o json | jq '.skills'
   ```
   If `coding-cli` is missing, distribute it:
   ```bash
   bash /opt/agentteams/agent/skills/worker-management/scripts/push-worker-skills.sh \
     --worker <worker-name> --skill coding-cli
   ```

2. **Add a "Coding CLI Mode" section to spec.md** (see template below).

---

## Step 3: Handling a `coding-request:` Message

When a Worker sends a message containing `coding-request:` (in their Worker Room or a Project Room):

**Parse the message:**
```
task-{task-id} coding-request:
workspace: /root/agentteams-fs/shared/tasks/{task-id}/workspace
---PROMPT---
{prompt content}
---END---
```

**Execute:**

```bash
# 1. Sync workspace from MinIO
task_id="task-YYYYMMDD-HHMMSS"
workspace="/root/agentteams-fs/shared/tasks/${task_id}/workspace"
mc mirror "${AGENTTEAMS_STORAGE_PREFIX}/shared/tasks/${task_id}/" "/root/agentteams-fs/shared/tasks/${task_id}/"

# 2. Check for processing marker (task coordination)
bash /opt/agentteams/agent/skills/task-coordination/scripts/check-processing-marker.sh "$task_id"
if [ $? -ne 0 ]; then
    # Another process is working on this task
    echo "Task ${task_id} is being processed by another operation. Retry later."
    exit 1
fi

# 3. Create processing marker
bash /opt/agentteams/agent/skills/task-coordination/scripts/create-processing-marker.sh "$task_id" "manager" 15

# 4. Save prompt to file
timestamp=$(date +%Y%m%d-%H%M%S)
prompt_dir="/root/agentteams-fs/shared/tasks/${task_id}/coding-prompts"
mkdir -p "$prompt_dir"
prompt_file="$prompt_dir/${timestamp}.txt"
cat > "$prompt_file" << 'PROMPT_EOF'
{extracted prompt content}
PROMPT_EOF

# 5. Get configured CLI
cli=$(jq -r '.cli' ~/coding-cli-config.json)

# 6. Run CLI
bash /opt/agentteams/agent/skills/coding-cli-management/scripts/run-coding-cli.sh \
  --cli "$cli" \
  --workspace "$workspace" \
  --prompt-file "$prompt_file" \
  --timeout 600
exit_code=$?

# 7. Remove processing marker
bash /opt/agentteams/agent/skills/task-coordination/scripts/remove-processing-marker.sh "$task_id"

# 8. On success (exit 0): push changes to MinIO
if [ "$exit_code" -eq 0 ]; then
    mc mirror "/root/agentteams-fs/shared/tasks/${task_id}/workspace/" "${AGENTTEAMS_STORAGE_PREFIX}/shared/tasks/${task_id}/workspace/" --overwrite
fi
```

**On success** — send to Worker in the same Room:
```
@{worker}:DOMAIN task-{task-id} coding-result:
CLI 工具已完成编码。请同步工作目录并 review 变更：
  agentteams-sync
变更记录：/root/agentteams-fs/shared/tasks/{task-id}/workspace/coding-cli-logs/
```

**On failure** (exit ≠ 0 or timeout) — see Step 4.

---

## Step 4: Handling Failure

**Notify Worker** in the task Room:
```
@{worker}:DOMAIN task-{task-id} coding-failed:
CLI 工具执行失败（exit code: {code}）。请自行完成编码任务。
你生成的提示词已保存于：/root/agentteams-fs/shared/tasks/{task-id}/coding-prompts/
```

**Notify Human Admin** via primary channel (see channel-management skill "Sending Messages to Primary Channel"):
```
Worker {worker-name} 的编码委托任务 {task-id} 中，{cli} 工具执行失败。

错误信息：{last lines from log file}

建议检查：
- ~/.{cli}/ 凭证是否有效（token 是否过期）
- /host-share/.{cli}/ 软链是否正常（ls -la /root/.{cli}）
- {cli} binary 是否在容器内可用（which {cli}）
- qwen 凭证在 ~/.qwen（settings.json 的 security.auth 或模型环境变量）；opencode 凭证在 ~/.config/opencode 或 ~/.local/share/opencode/auth.json（opencode auth login 存储 / provider 配置）
```

**Record in config** (optional, for heartbeat diagnostics):
```bash
jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg tid "$task_id" \
   '.last_failure = {task_id: $tid, failed_at: $ts}' \
   ~/coding-cli-config.json > /tmp/cfg.json && \
mv /tmp/cfg.json ~/coding-cli-config.json
```

---

## Spec.md Coding CLI Mode Template

Append to the end of spec.md when CLI mode is enabled:

```markdown
## Coding CLI Mode

本任务涉及代码修改。请使用 **Coding CLI 委托模式** 完成：

1. 克隆/准备代码到工作目录：`/root/agentteams-fs/shared/tasks/{task-id}/workspace/`
2. 推送到 MinIO：`mc mirror /root/agentteams-fs/shared/tasks/{task-id}/workspace/ ${AGENTTEAMS_STORAGE_PREFIX}/shared/tasks/{task-id}/workspace/`
3. 根据你的理解和 `coding-cli` skill 生成编码提示词，发送给我
4. 等待我执行 CLI 工具并返回结果
5. Sync 拉取变更：`agentteams-sync`
6. Review 变更并报告完成

如收到 `coding-failed:`，请自行完成编码工作。
```
