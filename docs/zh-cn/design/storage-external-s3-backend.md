# 外部 S3 兼容存储后端（`storage.provider=s3`）

完整文档见英文版：[`storage-external-s3-backend.md`](../../design/storage-external-s3-backend.md)。
本文件收录中文小节，随英文版增补同步。

## 托管 RustFS（单节点）

也可以不自行运维 S3 后端，而由 chart 直接部署单节点 RustFS
StatefulSet：镜像 tag 随 AgentTeams 发布 pin（与内置 MinIO 同一 pin
策略，`storage.rustfs.image.tag` 默认值与 `hack/mirror-images.sh`
条目始终一致）。

```yaml
# values.yaml
storage:
  provider: s3
  engine: rustfs              # chart 托管 RustFS（要求 provider=s3）
  bucket: agentteams-storage
  rustfs:
    image:
      tag: "1.0.4"            # 随发布 pin
    resources: {}
    persistence:
      size: 10Gi
    # 可选：引用已存在的 Secret（RUSTFS_ACCESS_KEY /
    # RUSTFS_SECRET_KEY），代替 values 内的默认凭据：
    # auth:
    #   existingSecret: my-rustfs-creds
```

controller 会自动接入 chart 内的 RustFS
（`AGENTTEAMS_FS_ENDPOINT=http://<release>-rustfs:9000`，凭据走
secretKeyRef）。`storage.engine=rustfs` 与外部端点互斥：engine 与
`storage.provider=minio`/`oss` 组合、与 `storage.mode=existing` 组合、
或 `storage.s3.endpoint` 非空时渲染直接失败——托管引擎部署自己的集群内
RustFS，端点始终由 chart 内的 StatefulSet 推导。若要指向外部
S3/RustFS，改用 `storage.engine=external`（默认）+
`storage.mode=existing` + `storage.s3.endpoint=<你的端点>`。

**版本 pin。** 与 MinIO 相同：RustFS 镜像 tag 随 AgentTeams 发布 pin。

**升级。** 改 `storage.rustfs.image.tag` 后 `helm upgrade`；数据在
StatefulSet PVC 上，升级保留（升级前确认 RustFS 版本的数据目录
兼容性）。

**回滚。** `helm rollback <release> <revision>` 回退到上一修订版本
（含 RustFS 镜像 tag）。

## 新装 vs. 存量切换

上面列出的后端选择（`minio` / `oss` / `s3` 外部 / `s3` +
`engine=rustfs`）定义的是**全新安装**部署什么，它们不是迁移机制。

对已运行的安装修改 `storage.provider`（或 `storage.engine`）**不是数据
迁移**：对象仍留在旧后端，新后端从空开始，切换窗口内的所有读写都会
中断。切换前需要自行搬完对象，再改 values。最小步骤：

1. 把每个 bucket 全量拷贝到目标（`mc mirror --overwrite --remove
   src/<bucket> dst/<bucket>`，逐 bucket 执行）。
2. 停写（缩容 Worker / 维护窗口）。
3. 修改 values（`storage.provider`、`storage.engine`、endpoint、
   凭据）并执行 `helm upgrade`。
4. 验证：controller 日志显示新的 provider，且一个 Worker 能通过新
   后端完成一次文件读写。

本 PR 不实现、也不测试切换本身——它不在本 PR 范围内。包含对账和回滚
的完整操作指南见英文版下方的「Migrating an embedded MinIO deployment
to an external backend」一节。

## 本地安装器边界

本地（embedded）安装器目前固定内置 MinIO，没有 RustFS 或外部 S3 选项。
本地安装选择 RustFS / 外部 S3 后端是配套 PR
（`feat/install-managed-rustfs`，stack 在 #1464 之上）。本 PR 不改变
本地安装行为。

## 验收

对应 review 清单的证据，范围以实际执行过的为准（集群级 e2e 尚未做，
见末尾遗留项）：

- **原 provider 兼容。** 默认 `minio + managed` 与 `s3`/`oss +
  existing` 渲染路径未改动；`tests/check-helm-agentteams.sh` 既有场景
  断言之（场景名："external S3 backend (inline credentials) renders
  endpoint + secret, no MinIO StatefulSet, no sidecar"、"external S3
  backend honours existingSecret + custom key names"、"s3 provider
  failure modes fail the render with explicit errors"）。默认 values
  的 `helm template` 输出与本 PR 前的 chart 逐字节一致。
- **RustFS 卷权限。** chart 托管形态 = 单节点 StatefulSet + PVC
  （`storage.rustfs.persistence.size`，默认 10Gi，`storageClassName`
  可配）；RustFS 数据目录权限由 RustFS 自身处理。已在 Raspberry Pi 5
  （aarch64）上对独立 RustFS 1.0.4 实例实测（2026-10-29）：controller
  的 `mc cp/cat/rm` 全部通过，Worker FileSync 只写 RustFS，
  Manager/Worker 重启回归通过。
- **管理 API / 用户策略。** controller 经 `mc` 创建 bucket 与
  per-worker 用户/策略；对 RustFS 1.0.3 验证过（既有事实，
  `values.yaml` 注释）。per-worker 文件访问（FileSync 写/读）另在 Pi
  实例的 1.0.4 上实测通过。
- **文件访问。** Pi 实测：controller S3 读写 + FileSync 单写 RustFS
  （split-brain 修复后）。
- **重启 / 升级路径。** Pi 实测 Manager/Worker 重启回归；StatefulSet
  PVC 跨 `helm upgrade` / `helm rollback` 保留（RustFS 数据目录不落
  镜像）；升级前先查 RustFS 版本的数据目录兼容性（见上文升级说明）。

**遗留项（如实列出）。**

- chart 托管 RustFS 的集群级 e2e（真实 `helm install` → PVC provision
  → secret → controller wiring 全链路）待下一个集群维护窗口执行。
- Pi 上 MinIO → RustFS 对象对账（5907 vs 5898 对象）未完成；对账
  完成前 MinIO 不退役。
