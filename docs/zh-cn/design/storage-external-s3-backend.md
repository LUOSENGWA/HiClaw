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
secretKeyRef）。`storage.engine=rustfs` 与
`storage.provider=minio`/`oss` 或 `storage.mode=existing` 组合时渲染
直接失败；显式设置 `storage.s3.endpoint` 时端点回到该外部值。

**版本 pin。** 与 MinIO 相同：RustFS 镜像 tag 随 AgentTeams 发布 pin。

**升级。** 改 `storage.rustfs.image.tag` 后 `helm upgrade`；数据在
StatefulSet PVC 上，升级保留（升级前确认 RustFS 版本的数据目录
兼容性）。

**回滚。** `helm rollback <release> <revision>` 回退到上一修订版本
（含 RustFS 镜像 tag）。
