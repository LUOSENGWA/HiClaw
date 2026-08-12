# Project / Workflow Inspection API

> Added by the project workflow inspection PR (agentteams/AgentTeams#1169).

The controller exposes two read-only endpoints that surface TeamHarness
project state (`shared/projects/{id}/meta.json`) as a LangGraph-aligned
workflow view. They are the data source for human-facing views (dashboard,
QwenPaw console plugin) and are consumed by `agt get projects`.

## Scope & prerequisites

These endpoints work in **any AgentTeams deployment** that runs TeamHarness
(projectflow/taskflow) on its workers — the storage layout is read through
the controller's configured object-store client, so `AGENTTEAMS_STORAGE_PREFIX`
and `AGENTTEAMS_FS_BUCKET` (including non-default values) are handled
automatically; no per-deployment code or configuration is required.

Prerequisites:

* TeamHarness MCP (`plugins/teamharness`) is installed on the workers that
  orchestrate projects. Only projects created by `projectflow`
  (`create_project` / `create_quick_project`) produce the
  `shared/projects/{id}/meta.json` that these endpoints read. Teams that
  manage tasks manually without projectflow have no project data — that is
  expected, not a bug.
* Project writes are pushed to shared storage by `_sync_project`
  (introduced alongside this API), so the controller sees near-live state
  rather than a startup snapshot.

Deployment modes (embedded Docker, incluster K8s) are all supported; in the
no-K8s development mode the controller skips authentication like the other
endpoints, so RBAC applies only when an authenticator is configured.

## Endpoints

### `GET /api/v1/projects`

List projects across all teams (and the global `shared/projects/` prefix).

Query parameters:

| Param | Meaning |
|:--|:--|
| `team` | Return only projects whose team matches. Team leaders are already scoped to their own team(s); standalone projects (empty team) only match when no filter is set. |

Response `200 OK`:

```json
{
  "projects": [
    {
      "project_id": "demo-project-001",
      "title": "Demo project",
      "status": "active",
      "plan_type": "dag",
      "team_id": "biz-team",
      "mode": "project"
    }
  ],
  "total": 1
}
```

* `status` is the raw project status written by TeamHarness:
  `active` | `paused` | `completed`.
* Projects are sorted by `project_id`. Duplicate ids across prefixes are
  de-duplicated (meta.json may be mirrored under both the effective team name
  and the CR name prefix).
* Projects with a missing or malformed `meta.json` are skipped (the directory
  may exist while the file is mid-write upstream).

### `GET /api/v1/projects/{id}/workflow`

Return the LangGraph-aligned workflow for one project.

Optional query parameter:

| Parameter | Type | Meaning |
|:--|:--|:--|
| `includeTasks` | `bool` | When `true`, also read each task's TaskMeta (`shared/tasks/{id}/meta.json`) and attach a `tasks_detail` array with spec/result/deliverable fields. Default `false` keeps the response lightweight. |

Response `200 OK`:

```json
{
  "project_id": "demo-project-001",
  "title": "Demo project",
  "status": "active",
  "plan_type": "dag",
  "team_id": "biz-team",
  "mode": "project",
  "source": "dingtalk",
  "nodes": [
    {"id": "t1", "name": "Task 1", "status": "completed", "assignee": "@w1:matrix.local"},
    {"id": "t2", "name": "Task 2", "status": "delegated", "assignee": "@w2:matrix.local"}
  ],
  "edges": [
    {"source": "t1", "target": "t2", "conditional": false}
  ],
  "next": ["t2"],
  "interrupts": [
    {"id": "t3", "value": "blocked"},
    {"id": "loop", "value": "waiting for human decision"}
  ],
  "values": {
    "project_id": "demo-project-001",
    "title": "Demo project",
    "status": "active",
    "plan_type": "dag",
    "team_id": "biz-team",
    "mode": "project",
    "task_count": {"completed": 1, "delegated": 1}
  },
  "loop": null,
  "requester": "dingtalk:user:session",
  "source_room_id": "!room:matrix.local",
  "tasks_detail": [
    {
      "task_id": "t1",
      "project_id": "demo-project-001",
      "status": "completed",
      "spec_path": "shared/tasks/t1/spec.md",
      "assigned_to": "@w1:matrix.local",
      "summary": "Alpha report done",
      "result_status": "SUCCESS",
      "deliverables": [{"type": "file", "path": "shared/tasks/t1/output.pdf"}],
      "result_path": "shared/tasks/t1/result.md"
    }
  ]
}
```

`tasks_detail` is only present when `?includeTasks=true`. It surfaces the
TaskMeta fields that the project-level `nodes[]` summary does not carry:
`spec_path` (task spec file), `summary` / `result_status` / `result_path`
(submission result), `deliverables` (artifact list) and `cancel_reason`.
TaskMeta is read from the same dual-prefix layout as projects
(`teams/{team}/shared/tasks/{id}/meta.json` first, then `shared/tasks/{id}/`),
so team-scoped tasks win over any global copy. Tasks without a TaskMeta file
(e.g. not yet delegated) are skipped; per-task read errors are skipped so one
bad task never fails the whole response.

Node statuses are normalized to a frontend-friendly enum:

| API value | Raw TeamHarness status |
|:--|:--|
| `pending` | `planned` |
| `delegated` | `assigned` |
| `in-progress` | `in_progress`, `submitted` |
| `completed` | `completed` |
| `revision` | `revision` |
| `blocked` | `blocked`, `cancelled` |

Semantics (mirror upstream `_ready_nodes` / `_ready_loop_nodes`):

* `next` — ready nodes: tasks whose raw status is `planned`/`assigned` and
  whose dependencies are all `completed`. Empty when the project is not active
  or a loop is `waiting_user` / `blocked` / `completed`.
* `interrupts` — human-decision waiting points: a blocked task, or a loop in
  `waiting_user` / `blocked` state.
* `values.task_count` — node counts per normalized status.

Error responses:

| Code | Meaning |
|:--|:--|
| `400` | Missing project id. |
| `403` | Authenticated but the role cannot read projects at all (e.g. Worker). |
| `404` | Project not found (no meta.json under any scanned prefix) — **or** the caller is a scoped reader (team leader / L2 human) who does not own the project (existence is hidden to prevent id enumeration). |
| `500` | K8s or object-store failure. |

### `GET /api/v1/projects/{id}/tasks/{taskId}/artifact`

Download one of a task's artifacts, completing the "deliverable → download →
review → accept" loop for dashboards and the console plugin.

Optional query parameter:

| Param | Meaning |
|:--|:--|
| `path` | The artifact path to download. Must be one of the task's **declared** artifacts — `result_path`, `spec_path` or an entry of `deliverables` (all read from TaskMeta). When omitted, the `result_path` (the published result) is served. |

Without `?path=` the artifact is the task's `result_path` (published result).
With `?path=` the requested path must be one of the task's declared artifacts
— `result_path`, `spec_path` (task spec) or a `deliverables` entry. The path
is then validated against a strict allowlist: it must be under
`shared/tasks/{taskId}/` or `shared/projects/{projectId}/`, and must not
contain `..` or start with `/`. Because the allowlist AND the declared-artifact
check both apply, a compromised worker cannot craft a path that reads
arbitrary MinIO objects, nor can a client download an undeclared file that
happens to live in the task directory.

The file is returned with `Content-Disposition: attachment` (filename =
basename, RFC 5987 `filename*=utf-8''...` for non-ASCII names so Chinese
filenames download correctly) and a `Content-Type` inferred from the
extension.

Error responses:

| Code | Meaning |
|:--|:--|
| `400` | Missing project id or task id. |
| `403` | Authenticated but the role cannot read projects at all (e.g. Worker). |
| `404` | Project not found / caller does not own it (existence hidden) / task not in the project graph / task has no published artifact / requested path is not a declared artifact / artifact file missing / artifact path rejected. |
| `500` | K8s or object-store failure. |

## Authentication & authorization

Two bearer-token paths are accepted (composite authenticator):

1. **Kubernetes service-account token** (TokenReview): admin / manager /
   worker. Team leaders (worker with `team_leader` role) see only their own
   team's projects.
2. **Matrix access token** (L2 humans): the token is validated with
   `GET /_matrix/client/v3/account/whoami`; the owning Matrix localpart is
   matched to a `Human` CR with `permissionLevel: 2` (Team). The human's
   `accessibleTeams` set is used as the multi-team scope — every team they
   control is aggregated into a single list/read view. Non-L2 humans
   (permissionLevel 1 or 3) are rejected.

Authorization matrix:

| Caller | List | Get workflow |
|:--|:--|:--|
| admin / manager | all teams | any project |
| team-leader (SA) | own team only | own team only |
| L2 human (Matrix) | all `accessibleTeams` | any accessible team |
| worker | denied | denied |

## `agt` CLI

`agt get projects [name]` wraps both endpoints:

```bash
agt get projects                      # list all
agt get projects --team biz-team      # filter by team
agt get projects demo-project-001     # workflow detail
agt get projects demo-project-001 -o json
agt get projects demo-project-001 --mermaid   # render DAG as mermaid
```

The CLI forwards whatever bearer token is configured (`AGENTTEAMS_AUTH_TOKEN`
or `AGENTTEAMS_AUTH_TOKEN_FILE`) verbatim, so an L2 human can also use it by
pointing either variable at their own Matrix access token — no separate CLI
auth mode is needed.
