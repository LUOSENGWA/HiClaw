# Task Completion Notification (submit_task)

> **Status**: Implemented in `plugins/teamharness/mcp/server.py` (branch
> `fix/task-completion-notification`).
> **Scope**: The TeamHarness MCP `taskflow` tool — the task lifecycle
> executed by Worker/Leader runtimes. The Manager-runtime hook variant
> (`copaw_worker.hooks.tools.taskflow`) is out of scope: the Manager
> coordinates tasks but is not a task executor, so its `submit_task`
> path is not the production completion route.

## Problem

The taskflow MCP layer is asymmetric:

- `delegate_task` **atomically** records the assignment in task state and
  publishes the assignment to the task room with `m.mentions`
  (`_send_delegate_notification`, stable txn `delegate-<task-id>`,
  recorded `eventId`, reuse-on-retry).
- `submit_task` records the terminal state and auto-publishes result
  artifacts as `m.file` events (`_publish_task_artifacts`), but the
  completion *message* is only a `notificationNeeded` **hint** — the
  Worker's LLM must self-remember to send
  `@leader TASK_COMPLETED: <task-id> - Result: shared/tasks/<task-id>/result.md`.

Real deployments (Node1, multi-turn sessions) show the Worker forgetting
that line after context compaction. Consequences:

- The Leader receives no wake signal: `check_task` is poll-based, and
  `m.file` artifact events carry no mention, so the Leader is not
  triggered by the artifacts alone.
- Downstream tasks stay stuck in `waiting` until the Leader happens to
  poll or a human pokes the room.

## Design

Mirror the existing delegate pattern, same file, same send path:

| Concern | `delegate_task` (existing) | `submit_task` (this change) |
|:--|:--|:--|
| Send helper | `_send_delegate_notification` | `_send_task_completion_notification` |
| Matrix path | HTTP PUT `/rooms/{room}/send/m.room.message/{txn}` (same as message tool) | identical |
| Credentials | `AGENTTEAMS_MATRIX_URL` + `AGENTTEAMS_WORKER_MATRIX_TOKEN` | identical (Worker's own token — the sender *is* the Worker) |
| Stable txn | `delegate-<task-id>` | `submit-<task-id>` |
| Mention | assignee mxid | **leader** mxid (resolved from runtime config) |
| Recorded event | task state `eventId` | task state `completionEventId` |
| Retry | reuse recorded `eventId` | reuse recorded `completionEventId` |
| Failure | task never marked `assigned` (hard fail) | **best-effort**: submission proceeds, response reports `sent: false` + error |

Message contract (first line parseable by leader-side prompts; mirrors
the task-execution skill):

```
@leader TASK_COMPLETED: <task-id> - Result: shared/tasks/<task-id>/result.md
- Worker: @worker:matrix.local
<summary preview, ≤500 chars>
```

```
@leader BLOCKED: <task-id> - <short blocker summary>
- Worker: @worker:matrix.local
```

`REVISION_NEEDED` / `INTERRUPTED` add a `- Status:` line; `SUCCESS` /
`SUCCESS_WITH_NOTES` / `BLOCKED` do not (the token already says it).

### Leader resolution

`_team_leader_matrix_id()` reads the runtime config the controller
projects into the Worker: `team.members[]` with
`role ∈ {team_leader, teamleader, leader}` → `matrixUserId` (same role
normalization as `_roomflow_room_meta`). Empty result → notification
skipped with a `skipped` reason (standalone runs).

### Membership guard

`_validate_assignee_membership(room_id, leader)` is reused as-is: when
the Matrix env is configured, the leader must be a joined member of the
task room; otherwise the send is skipped (reason recorded) instead of
producing an error event for a user who cannot receive it.

### Idempotency

- Stable txn `submit-<task-id>`: Matrix de-duplicates a redelivered
  identical PUT.
- `completionEventId` persisted in task state after the first success:
  a later resubmit returns the recorded event with `reused: true` and
  performs no HTTP call.

### Best-effort by contract

The completion message is a *notification*, not part of the state
mutation. Any problem (no room, no leader, no Matrix env, membership
missing, HTTP error) returns
`{"sent": false, "skipped"?: true, "error": "..."}` and the submit
still completes: `ok: true`, `status: "submitted"`, artifacts
published, state synced. The existing `notificationNeeded` hint is
**kept** — it also drives the requester reply-route report, which the
code-level line intentionally does not cover (different room/audience).

## Changes

| File | Change |
|:--|:--|
| `plugins/teamharness/mcp/server.py` | `_team_leader_matrix_id()`, `_send_task_completion_notification()`, `_task_completion_notification()`; `submit_task` branch calls the orchestrator after `_publish_task_artifacts` and adds `notification` to the response |
| `plugins/tests/teamharness/mcp/tools/test-taskflow.rb` | runtime config gains the team roster; fake Matrix server gains a `submit-` fault-injection branch; new assertions (see below); context file-event selection made mxcUri-based instead of positional (the last event is no longer guaranteed to be a file event) |

## Tests (contract, `test-taskflow.rb`)

1. **Send + content**: exactly one `submit-t-001` message event;
   `m.mentions.user_ids` contains the leader; body carries the contract
   line, `- Worker:` line, and the summary; auth = Worker token.
2. **Persistence**: task state `completionEventId` equals the response
   `notification.eventId`.
3. **Retry**: resubmit with the same payload returns
   `notification.reused: true` with the same `eventId` and sends no
   second event.
4. **BLOCKED**: `BLOCKED: <task-id> - <summary>` line, no
   `TASK_COMPLETED` text.
5. **Failure**: forced HTTP 500 on the `submit-` txn → submit still
   `ok: true` / `submitted`, `notification.sent: false` with the HTTP
   error, no `completionEventId` persisted.

## Open questions

1. **Should `check_task`'s polling be removed from leader prompts?** The
   auto-notification makes blind polling redundant; keep it as a
   reconciliation path (cheap) until the notification is proven in
   production.
2. **Manager-runtime taskflow**: the same asymmetry exists in
   `copaw_worker.hooks.tools.taskflow` (its `delegate_task` notifies, its
   `submit_task` does not). Not addressed here — the Manager is not a
   task executor; revisit if a deployment ever makes it one.
