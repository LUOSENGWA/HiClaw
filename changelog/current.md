# Changelog (Unreleased)

Record image-affecting changes to `manager/`, `worker/`, `copaw/`, `hermes/`, `openclaw-base/`, and `agentteams-controller/` here before the next release.

---

- fix(manager): make attached Worker Skill installation safe, deterministic, and independent of `assign_when` for explicit assignments ([04b6b9b](https://github.com/agentscope-ai/AgentTeams/commit/04b6b9bc10879cd5d9bea5df815a69828f67ec9f))
- fix(manager): restore canonical and distributed Worker Skill files after failed replacement and remove stale files ([71b6833](https://github.com/agentscope-ai/AgentTeams/commit/71b6833350a12f45cba88300c4dc104aebba61fc), [1ec5995](https://github.com/agentscope-ai/AgentTeams/commit/1ec5995892712491af704e1951e1164da2f9ef9f))

**What's New**

- **Project / workflow query API**: The Controller now exposes read-only project and workflow endpoints — `GET /api/v1/projects` and `GET /api/v1/projects/{id}/workflow` — backed by object storage (`shared/projects/{id}/meta.json`), so humans and frontends can inspect agent-orchestrated workflows (DAG/loop, ready nodes, interrupts). Projects created via TeamHarness `create_project` / `create_quick_project` now stamp `team_id` for team-scoped RBAC.

**Bug Fixes**

- None in this window.

---

**新增功能**

- **项目/工作流查询 API**：Controller 新增只读项目与工作流端点 `GET /api/v1/projects` 与 `GET /api/v1/projects/{id}/workflow`，数据源为对象存储（`shared/projects/{id}/meta.json`），使人或前端可以查看 Agent 编排的工作流（DAG/Loop、就绪节点、人工中断点）。通过 TeamHarness `create_project` / `create_quick_project` 创建的项目现在会写入 `team_id`，用于按团队隔离的 RBAC。

**Bug 修复**

- 本窗口无。

---

**Change list / 变更列表**

- `0de8a496` feat(controller): add project/workflow query API for human-visible workflows
