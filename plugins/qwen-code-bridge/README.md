# Qwen Code Bridge

把 **Qwen Code CLI** 接进 **QwenPaw**：派单、状态、唤醒、原生 Web 控制台、直聊、审批等级映射——一个本地插件，**与 AgentTeams 无关**（裸 QwenPaw、AgentTeams 里的 QwenPaw 都能用）。

- plugin id：`qwen-code-bridge`　后端包：`qwen_code_bridge`　HTTP 前缀：`/api/qwen-code-bridge`
- 版本矩阵：QwenPaw **2.2.x**（旧插件框架）与 **#7565 新框架**双绿；Qwen Code CLI **0.26.0**（web shell 按同版本重建，见 `web-shell/`）
- 测试：`78 passed + 1 skipped`（旧框架）/ `78 passed`（#7565）；发布门 `scripts/verify-bridge.sh`（双框架 + 4 项 live 检查 + evidence JSON）

## 目录

```
qwen-code-bridge/
├─ plugin.json              # 清单（entry.backend=plugin.py, entry.frontend=frontend/index.js）
├─ plugin.py                # 注册：http router / middleware / hooks / slash / prompt section
├─ qwen_code_bridge/
│  ├─ registry.py           # CLI 规格（qwen-code / opencode / qoder）
│  ├─ service.py            # probe / settings / install（本地 CLI）
│  ├─ router.py             # 管理面 + 派单面 + chat-meta + /web 反代/静态
│  ├─ dispatch.py           # spawn / watchdog / 认领旗 / 唤醒 / 审批映射
│  ├─ chats.py              # 侧栏分组与每单会话（原生 chats API）
│  ├─ slash.py              # /qcode（短路 agent，逐会话续聊）
│  ├─ runtime_hook.py       # PRE_DISPATCH 钩子：直聊/运行卡行为
│  ├─ prompt.py             # 内置提示词段（全英文）
│  ├─ millforge.py          # 千锻 M5 通知分层 + T5 coding-runs 投影
│  ├─ state.py / endpoints.py / web.py
├─ frontend/index.js        # 控制台路由 /qwen-code + core.chat 包装 + 主题同步
├─ web-shell/               # 重建 base 版 Qwen Code web shell 的补丁与构建脚本
├─ scripts/verify-bridge.sh # 发布门
└─ tests/                   # 76+1 / 77（见上）
```

## 安装

### A. 本地/任意 QwenPaw（推荐，热加载）

```bash
# 1) 放到 plugins 目录
cp -a qwen-code-bridge "${QWENPAW_WORKING_DIR:-/app/working}/plugins/qwen-code-bridge"
# 2) 官方安装接口（热加载，无需重启；loaded=true 即生效）
curl -X POST http://127.0.0.1:8088/api/plugins/install \
  -H 'Content-Type: application/json' -H "X-Agent-Id: $AGENT" \
  -d '{"source":"<绝对路径>/plugins/qwen-code-bridge","force":true}'
# 3) 验收
curl -s http://127.0.0.1:8088/api/plugins -H "X-Agent-Id: $AGENT" | grep qwen-code-bridge
```

> 注意：**拷贝目录本身不算安装**——必须走 `POST /api/plugins/install`（这是踩过的坑：拷目录不热加载，列表里看不见）。

### B. AgentTeams 部署（内置插件）

插件本身零耦合；AgentTeams 只是把它列为 builtin：

1. 插件目录位于 `AgentTeams/plugins/qwen-code-bridge/`（当前在分支 `agentteams/coding-cli-plugin`，ahead 36 未推）
2. 镜像：`qwenpaw/Dockerfile` 增加
   `COPY plugins/qwen-code-bridge/ /opt/agentteams/plugins/qwen-code-bridge/`，
   并把该路径加入 `install-builtin-qwenpaw-plugins.py` 的参数列表
3. worker 启动：`qwenpaw_worker/worker.py::_prepare_default_plugins` 已调用
   `_prepare_builtin_plugin("qwen-code-bridge", builtin_root / "qwen-code-bridge")`
4. 重建 worker 镜像 → 新 worker 启动即带插件（`/api/plugins` 显示 loaded）

### 3) 原生 Web 控制台（可选但推荐）

QwenPaw 单端口内跑**原生 Qwen Code web**需要一份按 base 重建的 shell：

```bash
sh web-shell/build.sh <SC/qwen-code 源码目录> [qwenpaw working 目录]
```

产物写入 `<state>/web-shell/dist`（约 35 MB，不入库）；插件检测到它即静态伺服，否则回退为 daemon 反代。上游 Qwen Code 升级时重建一次。

## 使用

| 入口 | 行为 |
|---|---|
| 左栏 **QwenCode** 组（置顶） | 每次派单一行：`<task> · <cli> · <state>`（终态改名） |
| 点任一派单会话 | 页面**整屏**变成原生 Qwen Code 控制台（无插件自建标题栏；`route.wrap("core.chat")` + 全屏 iframe） |
| `QwenCode · 直接对话` | 原生控制台（整屏）；**不需要前缀**，直接打字即由 Qwen Code 回答（PRE_DISPATCH 短路，逐会话续聊） |
| 任意聊天里 `/qcode <prompt>` | 零模型成本的直连回答（`--session-id` 首轮 / `-r` 续聊） |
| 审批 | QwenPaw 等级 → CLI tier：`STRICT→plan / SMART→auto-edit / AUTO→auto / OFF→yolo`（请求/env 可覆盖） |
| 门铃（可选） | `room_bell`：终态 3 行摘要投到房间（需部署侧有消息频道） |

派单 API：

```bash
curl -X POST http://127.0.0.1:8088/api/qwen-code-bridge/dispatch \
  -H 'Content-Type: application/json' -H "X-Agent-Id: $AGENT" \
  -d '{"task":"t1","prompt":"...","cwd":"/path","cli":"qwen","timeout":600,
       "origin":{"session_id":"<你的会话>","user_id":"<你>","channel":"console"},
       "wake":{"agent_id":"<agent>"}}'
```

- `origin` = **源会话三元组**（终态唤醒原样回放；与频道无关）
- 无 gates：并发/冲突闸已按 10/22 定案删除（模型侧排队）
- 观测：`GET /dispatch/{task}`（状态）、`/dispatch/{task}/log`（日志尾）、`<state>/web-proxy.log`（反代诊断，200 KB 上限）

## 维护

- **发布门**：`scripts/verify-bridge.sh`（每版本必跑；证据 JSON 落 `<state>/evidence/`）
- **web shell**：Qwen Code 版本升级 → 重跑 `web-shell/build.sh`（补丁见 `web-shell/README.md`）
- **状态目录**：`<workspace>/.qwenpaw/qwen-code-bridge/`（任务/会话映射/证据/重建产物）
- 已知边界：控制台内会话列表依赖“重建 base 版 shell”；未重建时只能用直聊/`/qcode`，见 `web.py` 的回退逻辑

## 排障

- **直接访问客户端路由并刷新出现 `{"detail":"Not Found"}`**：已修（10/22）；页面内已无“新标签打开”入口。浏览器导航到客户端路由（`/session/<id>`、`/threads`、`/settings`…）时，代理会在两种情况下都回退到 SPA：① 有重建产物 → 我们的 index；② 没有重建产物 → daemon 自己的 index（前缀改写）。若仍复现：硬刷新（Ctrl+Shift+R）并给出完整 URL。
- **控制台里会话列表为空**：说明跑的是**未重建**的 shell（`<state>/web-shell/dist` 不存在）——按 `web-shell/README.md` 构建一次，或从别的实例拷贝 dist。
- **`/web/settings` 返回 `Unknown coding CLI: web`**：已修（`/web/*` 路由前置，不再被 `/{cli_id}/settings` 吞）。
