# Model List API

Status: implemented
API: `GET /api/v1/models`

## Problem

Deployments that serve models through the AI gateway have no read-only way
to see which models are actually configured. The authoritative model list
lives in the gateway console as **AI routes** — a route name is the model
alias that Worker/Manager CRs reference in their model field. Until now,
listing them required console access (admin credentials), so API clients
holding only a controller token could not discover the valid model names
or see which consumers are authorized on each route.

The gateway's own `GET /v1/models` is **not** the authoritative list: the
ai-proxy plugin only matches `chat/completions` and `embeddings` traffic,
so that endpoint under-reports the configured models. The console's AI
route list is the source of truth.

## Design

`GET /api/v1/models` proxies the console AI route list through the
controller and returns one entry per route:

```json
{
  "models": [
    {
      "name": "qwen3-235b",
      "upstreams": [
        { "provider": "provider-a", "weight": 60 },
        { "provider": "provider-b", "weight": 40 }
      ],
      "allowedConsumers": ["manager", "worker-alice"]
    }
  ],
  "total": 1
}
```

- **`name`** — the AI route name, i.e. the model alias usable in
  Worker/Manager model fields.
- **`upstreams`** — the providers serving the route, with console weights.
  Omitted when the route has no upstreams.
- **`allowedConsumers`** — gateway consumers authorized on the route
  (from the route's `authConfig`). Omitted when empty.

Implementation notes:

- The console list endpoint returns route names only, so the client fetches
  each route individually for its upstreams and consumer allowlist — the
  same list-then-get pattern the consumer authorization code already uses.
- One unreadable route fails the whole call with a `502` rather than
  returning a silently incomplete catalog.
- Backends without a route-list API (the `ai-gateway` cloud provider) return
  `501`; deployments on those backends resolve models per-name instead.

## Authorization

The route is registered as `ActionGet` on the `gateway` resource kind. The
existing authorizer matrix already grants the `gateway` kind to
admin/manager only; team leaders, team-scoped humans, and worker accounts
fall through to the default deny. No authorizer change is required.

## Contract

`GET /api/v1/models` →

| Result | Code |
|--------|------|
| OK | `200` + model list (possibly empty) |
| gateway console unreachable / error | `502` |
| backend without route-list support, or no gateway configured | `501` |
| caller below L1 | `403` |
