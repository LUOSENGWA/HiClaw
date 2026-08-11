# 项目 / 工作流查看 API

> 由项目工作流查看 PR 新增（agentteams/AgentTeams#1169）。

Controller 提供两个只读端点，把 TeamHarness 项目状态
（`shared/projects/{id}/meta.json`）暴露为 LangGraph 对齐的工作流视图。
它们是面向人类视图（dashboard、QwenPaw console 插件）的数据源，
也被 `agt get projects` 消费。

## 端点

### `GET /api/v1/projects`

列出所有团队（以及全局 `shared/projects/` 前缀）的项目。

查询参数：

| 参数 | 含义 |
|:--|:--|
| `team` | 只返回团队匹配的项目。团队 leader 已被限定到自己的团队（们）；独立项目（空团队）仅在未设置过滤时匹配。 |

响应 `200 OK`：

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

* `status` 是 TeamHarness 写入的原始项目状态：
  `active` | `paused` | `completed`。
* 项目按 `project_id` 排序。跨前缀重复的 id 会去重（meta.json 可能同时镜像
  在 effective 团队名前缀和 CR 名前缀下）。
* meta.json 缺失或损坏的项目被跳过（目录可能存在而文件正在上游写入中）。

### `GET /api/v1/projects/{id}/workflow`

返回一个项目的 LangGraph 对齐工作流。

响应 `200 OK`：

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
  "source_room_id": "!room:matrix.local"
}
```

节点状态归一化为前端友好枚举：

| API 值 | 原始 TeamHarness 状态 |
|:--|:--|
| `pending` | `planned` |
| `delegated` | `assigned` |
| `in-progress` | `in_progress`、`submitted` |
| `completed` | `completed` |
| `revision` | `revision` |
| `blocked` | `blocked`、`cancelled` |

语义（镜像上游 `_ready_nodes` / `_ready_loop_nodes`）：

* `next` —— 就绪节点：原始状态为 `planned`/`assigned` 且依赖全部
  `completed` 的任务。项目非 active 或 loop 处于 `waiting_user` /
  `blocked` / `completed` 时为空。
* `interrupts` —— 等待人工决策点：blocked 任务，或 `waiting_user` /
  `blocked` 状态的 loop。
* `values.task_count` —— 按归一化状态统计的节点数。

错误响应：

| 状态码 | 含义 |
|:--|:--|
| `400` | 缺少项目 id。 |
| `403` | 团队 leader（或 L2 人类）访问非本团队项目。 |
| `404` | 项目不存在（所有扫描前缀下都无 meta.json）。 |
| `500` | K8s 或对象存储故障。 |

## 认证与授权

接受两种 bearer 令牌路径（复合认证器）：

1. **Kubernetes service account 令牌**（TokenReview）：admin / manager /
   worker。团队 leader（`team_leader` 角色的 worker）只能看自己团队的项目。
2. **Matrix 访问令牌**（L2 人类）：令牌用
   `GET /_matrix/client/v3/account/whoami` 验证；归属的 Matrix localpart
   匹配 `permissionLevel: 2`（Team）的 `Human` CR。人类的 `accessibleTeams`
   作为多团队范围——他们控制的所有团队聚合到单个列表/读取视图。非 L2 人类
   （permissionLevel 1 或 3）被拒绝。

授权矩阵：

| 调用方 | List | 获取工作流 |
|:--|:--|:--|
| admin / manager | 所有团队 | 任意项目 |
| team-leader（SA） | 仅自己团队 | 仅自己团队 |
| L2 人类（Matrix） | 所有 `accessibleTeams` | 任意可控团队 |
| worker | 拒绝 | 拒绝 |

## `agt` CLI

`agt get projects [name]` 包装两个端点：

```bash
agt get projects                      # 列出全部
agt get projects --team biz-team      # 按团队过滤
agt get projects demo-project-001     # 工作流详情
agt get projects demo-project-001 -o json
agt get projects demo-project-001 --mermaid   # 渲染 DAG 为 mermaid
```

CLI 原样转发配置的 bearer 令牌（`AGENTTEAMS_AUTH_TOKEN` 或
`AGENTTEAMS_AUTH_TOKEN_FILE`），所以 L2 人类也可以用——把任一变量指向自己的
Matrix 访问令牌即可，无需单独的 CLI 认证模式。
