# Coding Runs API (Read-Only Coding-Task Transcript Visibility)

A delegated coding task — a one-shot run of a coding CLI (Qwen Code / OpenCode
/ …) that a Manager, team Leader, or authorized Worker launched in a worker via
the `coding-cli` skill (#1342) — tees its full stdout (reasoning, tool calls,
file edits, exit reason) to a per-task log:

```
/root/agentteams-fs/shared/tasks/{task-id}/workspace/coding-cli-logs/run-{YYYYMMDD}-{HHMMSS}.log
```

…and `mc mirror`s the workspace to object storage. **The transcript already
exists and is already in MinIO**, but there was no read path. The Controller
therefore serves the coding-run transcripts **read-only**, mirroring the
worker chats proxy (`worker-chats-api.md`) — same thin, byte-transparent,
`404-not-403` discipline, applied to coding runs instead of agent
conversations. The controller does not mount the worker filesystem, so it reads
through `oss.StorageClient` (mc), exactly like the project task endpoints —
which is why this works in **both embedded and K8s** modes (it does not proxy a
per-worker console port).

**Read-only by design.** There is no create / delete / tail-stream route: the
run is already finished (one-shot), and the log is immutable once the CLI exits.

## Routes

| Route | Storage read | Returns |
|-------|-------------|---------|
| `GET /api/v1/coding-runs` | `ListObjects` over the caller's visible `…/coding-cli-logs/` prefixes | `list[{task_id, run, team}]` — the runs the caller may read (bounded by a 200-run cap) |
| `GET /api/v1/coding-runs/{taskID}/runs/{run}` | `GetObject …/coding-cli-logs/{run}` | the raw `.log`, byte-transparent (`text/plain`) |

- **Data layout (dual-prefix)**, read via `oss.StorageClient`:
  - standalone task: `shared/tasks/{taskID}/workspace/coding-cli-logs/run-*.log`
  - team task: `teams/{team}/shared/tasks/{taskID}/workspace/coding-cli-logs/run-*.log`
- **Path segments are validated before any storage read**: `taskID` must pass
  `isSafeTaskID` (alphanumeric, no leading `.`/`_`/`-`, no `/`) and `run` must
  match `run-\d{8}-\d{6}\.log` — neither can carry path traversal or address a
  file other than a run transcript. Failures return `400`.
- **Read-only** (`text/plain`, no body cap — a transcript can be large; the
  project task artifact route has the same posture).

## Authorization

Two layers, the standard AgentTeams split (authorizer admits the *action*; the
handler enforces the *scope*).

### Authorizer layer (`internal/auth/authorizer.go`)

`GET /api/v1/coding-runs` is registered under `ActionGet, "coding-runs"`.

| Role | `get` / `list` coding-runs |
|------|---------------------------|
| L1 admin | allowed (full access) |
| manager SA | allowed (full access, same as the project endpoints) |
| team leader | **allowed** (handler scopes to own team) |
| L2 human | **allowed** (handler scopes to `accessibleTeams`) |
| worker | **denied** (workers run the CLIs; they do not read transcripts through the API) |

The `coding-runs` case is explicit in `authorizeTeamLeader` / `authorizeHuman`
(read allowed, everything else denied). Without it, L2 callers fall through to
the authorizer's `default: deny` and never reach the handler — the handler's
team-scope logic would be dead code and real L2 requests would `403`.

### Handler layer (team scope + W4, `CodingRunsHandler`)

- **L1 / manager** read every prefix: all teams **plus** the global standalone
  `shared/tasks/` prefix.
- **L2 team leader / human** read only the prefix of each team they belong to
  (`visibleTaskPrefixes` narrows the dual-prefix set). **Standalone tasks have
  no team ownership and stay L1-only** — an L2 caller can neither list nor read
  a `shared/tasks/…` run.
- **W4 anti-probing**: a run the caller cannot see returns the **same `404` as a
  missing run** — task/run existence across teams cannot be probed (never
  `403`).
- The optional `?team=` query narrows a detail read to one team's prefix
  (disambiguation); it is a filter, never authorization — the team-scope
  boundary above still applies.

## Status mapping

| Situation | Response |
|-----------|----------|
| `taskID` / `run` fails validation | `400` |
| run not found, or not in the caller's scope (W4) | `404` `{"message":"coding run not found"}` |
| no runs the caller may read | `200 []` (empty list, never `null`) |
| caller denied at the authorizer (worker) | `403` |
| storage read error mid-list | skipped for that prefix (the rest of the list still returns) |

## Example

```bash
# L1 admin lists every coding run (team + standalone)
curl -s http://127.0.0.1:8090/api/v1/coding-runs -H "Authorization: Bearer $ADMIN_TOKEN"
# → 200 [{"task_id":"t1","run":"run-20261012-120000.log","team":"alpha-team"}, …]

# team leader (alpha) sees only alpha-team runs; standalone + other teams absent
curl -s http://127.0.0.1:8090/api/v1/coding-runs -H "Authorization: Bearer $ALPHA_LEAD_TOKEN"
# → 200 [{"task_id":"t1","run":"run-20261012-120000.log","team":"alpha-team"}]

# open one run's transcript (byte-transparent)
curl -s http://127.0.0.1:8090/api/v1/coding-runs/t1/runs/run-20261012-120000.log \
  -H "Authorization: Bearer $ADMIN_TOKEN"
# → 200 "[run-coding-cli] cli=qwen workspace=…\n<full CLI transcript>"

# cross-team / standalone read as an L2 caller → 404, indistinguishable from absent
curl -s http://127.0.0.1:8090/api/v1/coding-runs/t9/runs/run-20261012-120100.log \
  -H "Authorization: Bearer $ALPHA_LEAD_TOKEN"
# → 404 {"message":"coding run not found"}

# traversal attempt → 400 (rejected before any storage read)
curl -s "http://127.0.0.1:8090/api/v1/coding-runs/../etc/runs/run-20261012-120000.log" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
# → 400 {"message":"invalid task id"}
```

## Notes

- **Data source is #1342** (the `coding-cli` skill's `run-coding-cli.sh`). Until
  #1342 merges, the log files do not exist yet, so the endpoints return an empty
  list / `404` — graceful, no error. This surface is the read side only and
  needs no #1342 code.
- **One-shot runs only** (#1342's current form). Real-time, serve-based
  sessions belong to the QwenCode runtime line (#1357, deferred) — a separate,
  later surface.
- **Transcript sensitivity**: the log is byte-transparent and is whatever the
  CLI printed — if the delegating agent put a secret in the prompt or a tool
  echoed one, it appears here. Access is bounded by the team scope above, but
  the "secrets do not go in prompts" responsibility sits with the delegating
  agent (the same contract the coding skill states); server-side redaction of
  arbitrary CLI stdout is out of scope for v1.
- **Dashboard consumption** is a separate follow-up (mirrors how the worker-chats
  dashboard panel consumes `worker-chats-api.md`).

## Tests

- `internal/server/coding_runs_test.go` — over the `mcLikeOSS` mc-ls
  directory-view fake + fake K8s client:
  - list: `TestListCodingRuns_AdminSeesTeamAndStandalone`,
    `TestListCodingRuns_L2SeesOnlyOwnTeam`,
    `TestListCodingRuns_SkipsNonRunFiles`
  - detail: `TestGetCodingRun_AdminReadsByteTransparent`,
    `TestGetCodingRun_L2OwnTeam`, `TestGetCodingRun_L2OtherTeam` (W4 404),
    `TestGetCodingRun_L2CannotSeeStandalone` (L1-only),
    `TestGetCodingRun_InvalidTaskID` (traversal 400),
    `TestGetCodingRun_InvalidRun` (400), `TestGetCodingRun_NotFound` (404)
- `internal/auth/authorizer_test.go` — `TestAuthorizer_CodingRunsScoped` pins
  the authorizer boundary: L2 human + team leader `get`/`list` allowed (so the
  handler scope runs), worker denied.
