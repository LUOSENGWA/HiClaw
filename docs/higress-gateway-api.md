# Higress Gateway API Reference

AgentTeams embeds [Higress](https://github.com/alibaba/higress) as its AI gateway and
API gateway. This document describes the interfaces Higress exposes to the outside
world (the **data plane**) and the console APIs AgentTeams itself uses to manage the
gateway (the **control plane**).

- **Data plane** — endpoints that Workers, Managers, and external clients call to reach
  LLM providers, MCP servers, exposed Worker ports, and bundled services.
- **Control plane** — the Higress Console REST API that the `agentteams-controller`
  and legacy Manager scripts use to configure routes, consumers, and MCP servers.

## Default domains and ports

| Resource | Default domain | In-container port | Host port (installer) |
|----------|----------------|-------------------|------------------------|
| AI Gateway (LLM + MCP) | `aigw-local.agentteams.io` | `8080` | `18080` (`AGENTTEAMS_PORT_GATEWAY`) |
| Higress Console API | (controller-internal) | `8001` | `18001` (`AGENTTEAMS_PORT_CONSOLE`) |
| Matrix homeserver | `matrix-local.agentteams.io` | `8080` | `18080` |
| Element Web | `matrix-client-local.agentteams.io` | `8080` (via gateway) / `8088` (direct) | `18080` (via gateway) / `18088` (direct, `AGENTTEAMS_PORT_ELEMENT_WEB`) |
| MinIO file system | `fs-local.agentteams.io` | `9000` (MinIO S3 API, **not** gateway `8080`) | via gateway (`18080`); no direct host mapping |
| OpenClaw Console | `console-local.agentteams.io` | `8080` (via gateway) / `18888` (direct) | `18080` (via gateway) / `18888` (direct) |

> **Port note**: inside the `agentteams-net` Docker network (i.e. from a Worker or
> Manager container) the gateway listens on **`:8080`**. The installer publishes it to
> the host as **`:18080`**. Prefer the in-container form when writing Worker
> configuration (`AGENTTEAMS_AI_GATEWAY_URL=http://aigw-local.agentteams.io:8080`).

## Data plane — externally callable endpoints

### 1. LLM OpenAI-compatible API

The AI route `default-ai-route` (path prefix `/v1`, upstream selected by
`AGENTTEAMS_LLM_PROVIDER`) exposes the OpenAI-compatible LLM endpoints. Requests must
carry the caller's consumer key.

```
POST /v1/chat/completions
GET  /v1/models
```

Example (run inside a Worker container):

```bash
curl -sf http://aigw-local.agentteams.io:8080/v1/models \
  -H "Authorization: Bearer ${AGENTTEAMS_WORKER_GATEWAY_KEY}"
```

Authentication is per-identity **key-auth** (Bearer). Each Manager/Worker consumer is
registered in Higress with its own `GatewayKey`, and is only allowed on AI routes that
list it in `authConfig.allowedConsumers`. This is managed by the controller through
`AuthorizeAIRoutes` / `DeauthorizeAIRoutes` (see
`agentteams-controller/internal/gateway/higress.go`).

### 2. MCP server endpoints

Each MCP server registered in Higress is exposed under `/mcp-servers/{name}/mcp` on the
AI Gateway domain. The name is the MCP server name — for the bundled GitHub MCP server
this is `mcp-github`.

```
POST /mcp-servers/{name}/mcp
```

Example (run inside a Worker container):

```bash
mcporter --transport http \
  --server-url "http://aigw-local.agentteams.io:8080/mcp-servers/mcp-github/mcp" \
  --header "Authorization=Bearer ${AGENTTEAMS_WORKER_GATEWAY_KEY}" \
  call list_repos '{"owner": "test"}'
```

MCP access is also governed by per-consumer authorization (`consumerAuthInfo` on the
MCP server). Registration is handled by the controller (embedded stacks) or by the
legacy `setup-higress.sh` / `setup-mcp-server.sh` scripts (≤v1.0.9 Manager images);
see `manager/agent/skills/mcp-server-management/`.

### 3. Exposed Worker ports (service publishing)

A Worker whose `spec.expose` lists ports gets a gateway route with an auto-generated
domain, so its HTTP services become reachable from outside the container.

Auto-generated domain pattern:

```
worker-{name}-{port}-local.agentteams.io
```

Example: worker `alice` exposing port `8080` → `http://worker-alice-8080-local.agentteams.io`.

Exposed routes have **no authentication** (public access by design); the controller
creates the Higress domain, service source, and route during reconciliation
(`ReconcileExpose` in `agentteams-controller/internal/service/provisioner_expose.go`).
See `manager/agent/skills/service-publishing/SKILL.md` for usage.

### 4. Bundled service routes

The installer also registers routes for the services bundled with the embedded stack:

| Route | Domain | Path | Backend |
|-------|--------|------|---------|
| Matrix homeserver | any (`domains: []`) | `/_matrix` | Tuwunel (`tuwunel.static:6167`) |
| Element Web | `matrix-client-local.agentteams.io` | `/` | `element-web.static:8088` |
| HTTP file system | `fs-local.agentteams.io` | `/` | MinIO S3 (`minio.static:9000`) |
| OpenClaw Console | `console-local.agentteams.io` | `/` | `openclaw-console.static:18888` (basic-auth) |

These are created once on first boot by `setup-higress.sh` (non-idempotent, marker
protected) or by the controller initializer on embedded stacks.

## Control plane — Higress Console API

The controller and legacy scripts manage the gateway through the Higress Console REST
API (in-container `http://127.0.0.1:8001`). Session-cookie auth: `POST /system/init`
bootstraps the admin account, `POST /session/login` obtains the cookie.

| Endpoint | Method(s) | Purpose |
|----------|-----------|---------|
| `/system/init` | POST | Initialize admin account (first boot) |
| `/session/login` | POST | Login, obtain session cookie |
| `/user/changePassword` | POST | Rotate admin password |
| `/v1/consumers` | GET, POST | List / create key-auth consumers |
| `/v1/consumers/{name}` | DELETE | Remove a consumer |
| `/v1/ai/routes` | GET, POST | List / create AI routes |
| `/v1/ai/routes/{name}` | GET, PUT | Read / update an AI route (incl. `authConfig.allowedConsumers`) |
| `/v1/ai/providers` | GET, POST | List / create LLM providers |
| `/v1/ai/providers/{name}` | GET, PUT | Read / update a provider |
| `/v1/domains` | POST | Create a domain |
| `/v1/domains/{name}` | DELETE | Remove a domain |
| `/v1/service-sources` | GET, POST | List / create service sources |
| `/v1/service-sources/{name}` | PUT, DELETE | Update / remove a service source |
| `/v1/routes` | GET, POST | List / create classic routes |
| `/v1/routes/{name}` | PUT, DELETE | Update / remove a classic route |
| `/v1/mcpServer` | GET, PUT | List / upsert MCP servers |
| `/v1/mcpServer/consumers` | GET, PUT | Query / authorize consumers on an MCP server |
| `/system/higress-config` | GET, PUT | Read / patch gateway config (e.g. stream `idleTimeout`) |

Consumer authorization on AI routes is the responsibility of the reconcilers — the
initializer never writes `authConfig.allowedConsumers` (see `EnsureAIRoute` in
`agentteams-controller/internal/gateway/higress.go`).

## Related

- [Architecture overview](architecture.md) — role of Higress in the system.
- [Worker guide](worker-guide.md) — troubleshooting LLM / MCP connectivity from a Worker.
- [Kubernetes-native orchestration](k8s-native-agent-orch.md) — LLM/MCP security model.
- [Development](development.md) — Higress configuration guidance for contributors.
