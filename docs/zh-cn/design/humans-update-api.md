# Human Update API

状态：已实现
API：`PUT /api/v1/humans/{name}`

## 问题

Human 生命周期有 create / get / list / delete，但没有 update。变更一个人的权限等级、团队范围或 worker 范围，需要删除并重建 Human CR——这会重新置备 Matrix 账户并发放新的一次性口令，摧毁用户已登录所用的身份。同时，随着团队的组建与解散，也没有办法收窄或扩展现有的授权。

## 设计

`PUT /api/v1/humans/{name}` —— 对 `spec` 可变部分的 merge-patch：

- **指针语义**（既有 `containerManaged` / `state` 模式）：缺省字段 = 不变；在场字段 = 替换；显式空列表 = 清空列表。
- **可变字段：** `displayName`、`email`、`permissionLevel`、`accessibleTeams`、`accessibleWorkers`、`capabilities`、`note`。
- **经本端点不可变：** `name` 与 Matrix 身份（username / matrixUserID）。账户重新置备是有意的销毁-重建操作，不是编辑。
- **等级语义（API 授权）：** `1` = admin（Matrix 令牌路径不解析 level-1 人类；他们使用 admin SA）。`2` = 团队范围（`accessibleTeams` + `capabilities`；见 [l2-worker-scoped-write.md](l2-worker-scoped-write.md) 与 [capability-foundation.md](capability-foundation.md)）。`3` = worker 范围——对且仅对 `accessibleWorkers` 的**只读**访问（worker 详情、通道配置、审批配置；无写）；level-3 CR 上的 `accessibleTeams` / `capabilities` 不授予任何东西（见 [l3-worker-scoped-read.md](l3-worker-scoped-read.md)）。等级于下次认证时生效（缓存身份按认证器的正常 TTL 过期）。
- **校验，先于 K8s 写入应用：**
  - `permissionLevel` 必须为 1、2 或 3 → 否则 `400`。
  - `accessibleTeams` 必须引用存在的 Team CR，`accessibleWorkers` 必须引用存在的 Worker CR → 否则 `400` 并点名缺失引用。悬空授权虽不会静默扩大权限，却会让人无法触达其自认为可及的资源；在写入时拒绝，保持权限模型的诚实。
  - `capabilities` 必须落在封闭的五值集合内（`full_access`、`channel_secrets`、`external_sources`、`approval_policy`、`secret_reveal`——见 [capability-foundation.md](capability-foundation.md)）→ 否则 `400` 并点名未知值、列出合法集合。以规范化形式存储（去重 + 排序）；每次授予/撤销均写入双层审计轨迹。
- **授权：** 该路由是 `human` 类型的 `ActionUpdate`。既有矩阵已仅允许 admin/manager；团队 leader、团队范围人类与 worker 账户均落入默认拒绝。无需改动授权器。
- **调和集成：** 写操作更新 Human CR；既有 human 调和器（identity / infra / rooms 各阶段）在其正常周期内重新同步 Matrix 邀请、房间成员资格与 `groupAllowFrom`。无新调和路径。
- **范围之外——房间权限等级：** 本端点不授予或撤销 Matrix 房间管理权。`permissionLevel` 变更对 API 授权立即生效，但本身不改变该人类在既有房间中已持有的权限等级；为人类成员调和房间权限等级是另一关切，本端点及其调和集成不予处理。

## 契约

`PUT /api/v1/humans/{name}` →

| 结果 | 代码 |
|--------|------|
| 已更新 | `200` + 完整 human 表示 |
| human 不存在 | `404` |
| 非法 JSON / 等级非法 / 悬空引用 / 未知 capability | `400` |
| 引用校验后端失败（K8s 错误） | `500` |
| 重试后仍 K8s 冲突 | `409` |

请求体（所有字段可选）：

```json
{
  "displayName": "Alice",
  "email": "alice@example.com",
  "permissionLevel": 2,
  "accessibleTeams": ["market-team"],
  "accessibleWorkers": [],
  "capabilities": ["approval_policy"],
  "note": "Marketing lead"
}
```

## 范围之外

- Matrix 身份变更（账户重新置备）。
- 由人类本人自助更新（L2 人类更新自身授权将构成提权路径；仅 admin 是安全默认）。
- 批量更新。

## 测试

- `internal/server/resource_handler_human_update_test.go` — 等级 + 团队更新生效且未触及字段保留；部分合并保留其余；显式空列表清空；非法等级（0/4/-1）→ 400；团队不存在 → 400 点名；worker 不存在 → 400 点名；worker 存在 → 200；human 未知 → 404。Capabilities：授予生效（去重 + 排序）且其他字段保留；缺省 = 不变；`[]` 清空；未知值 → 400 并列出合法集合（且不落库）；授予 + 撤销写入预期的双层审计行。
- `internal/auth/authorizer_test.go` — `TestAuthorizer_HumanUpdateAdminOnly`：admin/manager 允许；团队 leader、L2 人类、worker 拒绝。
