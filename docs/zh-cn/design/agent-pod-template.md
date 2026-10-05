# Agent Pod 模板

agentteams-controller 为每个 Manager 和每个 Worker 创建一个 Kubernetes Pod（"agent Pod"）。默认情况下，这些 Pod 形态极简：一个名为 `worker` 的容器、一个投影的 `agentteams-token` 卷、控制器管理的 `ServiceAccount`，其余寥寥。

要注入集群特定关切——sysctls、nodeSelectors、tolerations、imagePullSecrets、被 CNI/sidecar 注入器消费的 annotations 等——请通过 ConfigMap 提供一个 `corev1.PodTemplateSpec` 覆盖层。

## 工作方式

在**每一次** `Create()` 中，控制器从自身 namespace 读取一个 ConfigMap，其名称等于其 `AGENTTEAMS_CONTROLLER_NAME` 环境变量（对 Helm release `prod` 即 `prod-agentteams-controller`）。若 ConfigMap 存在且含键 `pod-template.yaml`，其值被解析为 `PodTemplateSpec`，与控制器自有字段合并，产出最终 Pod。

> `AGENTTEAMS_CONTROLLER_NAME` 同时是 leader 选举租约名，也是控制器在其创建的每个 Worker/Manager/Team/Human CR 上以 `agentteams.io/controller` 标签标注的值。控制器的 informer 缓存按此标签过滤 CR，故同一 namespace 中多个 AgentTeams release 永不互相调和对方的资源。Helm chart 会依据 release 名自动设置；手工部署时请显式设置——incluster 模式下缺省启动的控制器会快速失败。

无缓存。编辑 ConfigMap → 控制器下一个创建的 Pod 即采用新模板。既有 Pod 不受影响（删除它们才会拾取变更）。

若 ConfigMap 缺失、格式错误或 API 调用因任何原因失败，控制器回退到默认 Pod 形态。Pod 创建永不被损坏的模板阻塞。

## ConfigMap 模式

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: <controller-name>    # == AGENTTEAMS_CONTROLLER_NAME env on controller
  namespace: <controller-ns> # same namespace as controller
data:
  pod-template.yaml: |
    metadata:
      annotations: {...}
      labels: {...}
    spec:
      nodeSelector: {...}
      tolerations: [...]
      imagePullSecrets: [...]
      securityContext: {...}
      # ...any corev1.PodSpec field
```

> `pod-template.yaml` 下的值是仅有 `metadata:` 与 `spec:` 两个顶层字段的 `PodTemplateSpec`。**不要**用 `apiVersion: v1` / `kind: PodTemplate` 包裹。

现成可应用的示例见 [`docs/examples/agent-pod-template-cm.yaml`](../examples/agent-pod-template-cm.yaml)。

## 合并语义

**模板胜**的字段：

- `spec.nodeSelector`
- `spec.tolerations`
- `spec.affinity`
- `spec.imagePullSecrets`
- `spec.securityContext`（含 `sysctls`）
- `spec.topologySpreadConstraints`
- `spec.runtimeClassName`、`spec.schedulerName`、`spec.priorityClassName`
- `spec.dnsPolicy`、`spec.dnsConfig`
- `spec.hostAliases`（之后追加控制器的 `CreateRequest.ExtraHosts`）
- 名称非 `worker` 的 `spec.containers[]` —— 作为 sidecar 保留
- 下节未列出的任何其他 `spec.*` 字段

**控制器胜**的字段（模板提供的值被丢弃）：

- `metadata.ownerReferences` — 恒继承自控制器 Pod
- `spec.serviceAccountName`
- `spec.automountServiceAccountToken` — 强制为 `false`
- Agent 容器的 `image`、`env`、`workingDir`、`imagePullPolicy`

混合合并：

| 字段 | 规则 |
|---|---|
| `metadata.labels` | 模板在前，键冲突时控制器标签覆盖 |
| `metadata.annotations` | 模板在前，键冲突时控制器注解覆盖 |
| Agent 容器的 `resources` | `CreateRequest.Resources`（按请求）> 模板的 resources > 后端默认 |
| Agent 容器的 `volumeMounts` | 模板在前，`agentteams-token` 的 volumeMount 恒被追加 |
| `spec.volumes` | 模板在前，`agentteams-token` 投影卷恒被追加 |
| `spec.restartPolicy` | 模板设置则用之，否则 `Always` |

## 排障

**我的模板生效了吗？** 新建一个 Worker 并检查其 Pod：

```bash
kubectl get pod <worker-pod> -n <ns> -o yaml | grep -A5 nodeSelector
```

**控制器看到我的 ConfigMap 了吗？** 控制器日志在 `V(1)` 或默认级别会显示其一：

- `agent pod template ConfigMap not found; using empty overlay` —— 创建/重命名 CM。
- `agent pod template YAML parse failed` —— 你的 YAML 非法。
- `agent pod template ConfigMap fetch failed` —— API / RBAC 问题。

**RBAC**：Helm 默认安装的控制器 `ClusterRole` 已授予对 `configmaps` 的 `get`。手工编写的 Deployment 请确保该动词在场。
