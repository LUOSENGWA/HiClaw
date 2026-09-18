# Worker Chats API (Read-Only Session Visibility)

The worker's qwenpaw app stores every conversation (a *chat*) per
`(user, channel)` — the Matrix room thread, the QQ direct messages, the
console sessions — and exposes it read-only on the worker's console port
(`8088`, no auth in worker context). That port is not published, so
scoped callers (L2 humans, team leaders) have no way to see a worker's
conversation history through the controller.

The Controller therefore proxies three **read-only** subpaths of the
worker's chat API. The proxy is thin and byte-transparent (the same
pattern as the worker checkpoint and channel proxies): it resolves the
worker, enforces the worker-scoped read boundary, and forwards to the
worker's qwenpaw app over the shared docker network. The dashboard
renders the results as the worker's session list / transcript / run
status.

**Read-only by design.** There is no create / archive / delete / stream
route: the conversation lifecycle stays on the qwenpaw app and its own
channels.

## Routes

| Route | Upstream (worker qwenpaw app) | Returns |
|-------|------------------------------|---------|
| `GET /api/v1/workers/{name}/chats` | `GET /api/chats` | `list[ChatSpec]` — id, name, user_id, channel, created/updated, pinned, archived, … |
| `GET /api/v1/workers/{name}/chats/{chat_id}` | `GET /api/chats/{chat_id}` | `ChatHistory{messages: [Message], status}` — the full transcript |
| `GET /api/v1/workers/{name}/chats/{chat_id}/status` | `GET /api/chats/{chat_id}/status` | `ChatStatusResponse{status: "idle" \| "running"}` (QwenPaw ≥ 2.2.1) |

- `{chat_id}` is the qwenpaw chat id — always a lowercase UUIDv4 (every
  chat is created with `str(uuid4())`). Anything else is rejected with
  `400` before the dial (no path injection possible).
- The list endpoint forwards the documented read-only filters
  `?user_id=`, `?channel=`, `?archived=`, `?include_app_owned=`
  (whitelist; unknown parameters → `400`, never `422`). The detail and
  status routes take no query parameters.
- 2xx responses stream verbatim (a transcript can be large; no body cap,
  same as the checkpoint proxy). Upstream 4xx responses pass through
  verbatim (see *QwenPaw version contract*). Upstream 5xx is wrapped in
  a `502` with a truncated body.

## Authorization

The routes ride on the standard worker `GET` authorization (L1 admin:
any worker; L2 human / team leader: own-team workers via the
`TeamMatches` scope check; standalone humans: `404`). Cross-team access
returns `404`, uniformly with "no such worker" — worker existence cannot
be probed (404-not-403, same as the other worker-scoped reads).
Embedded mode only: kube mode returns `503` uniformly.

**Data sensitivity.** A transcript is the worker's conversation context
verbatim and may contain tool output or fragments of stored
configuration. Access is bounded by the worker's existing team scope —
no new credential or capability is introduced, and callers outside the
team cannot even confirm the worker exists.

## Status mapping

| Upstream / situation | Controller response |
|----------------------|---------------------|
| worker name / chat id fails validation | `400` |
| worker not found | `404` `{"message":"worker not found"}` |
| scoped caller outside the worker's team | `404` (same body as unknown worker) |
| kube mode | `503` |
| upstream unreachable (5 s timeout) | `502` `{"message":"worker unreachable"}` |
| upstream 2xx | `200`, body + `Content-Type` verbatim |
| upstream 4xx | passed through verbatim (status + body) |
| upstream 5xx | `502`, body truncated to 128 bytes |

## Example

```bash
# L2 human lists the sessions of a worker in their team
curl -s http://127.0.0.1:8090/api/v1/workers/daily-carol/chats \
  -H "Authorization: Bearer $AGENTTEAMS_MATRIX_TOKEN"
# → 200 [{"id":"0d9f…","name":"matrix:…","user_id":"alice@…","channel":"matrix",…}, …]

# filter to one user / channel
curl -s "http://127.0.0.1:8090/api/v1/workers/daily-carol/chats?channel=matrix" \
  -H "Authorization: Bearer $AGENTTEAMS_MATRIX_TOKEN"

# open the transcript of one session
curl -s http://127.0.0.1:8090/api/v1/workers/daily-carol/chats/0d9f3d6e-…-0e1f \
  -H "Authorization: Bearer $AGENTTEAMS_MATRIX_TOKEN"
# → 200 {"messages":[{"type":"message","role":"user","content":[…],…}], "status":"idle"}

# is the worker currently replying in that session? (QwenPaw ≥ 2.2.1)
curl -s http://127.0.0.1:8090/api/v1/workers/daily-carol/chats/0d9f3d6e-…-0e1f/status \
  -H "Authorization: Bearer $AGENTTEAMS_MATRIX_TOKEN"
# → 200 {"status":"running"}   (always 200 on 2.2.1+; 404 on older builds)
```

## Notes

- **Single-agent workers.** Without an `X-Agent-Id` header the worker's
  qwenpaw app resolves the active agent from its config; in a
  single-profile worker container that is the worker's own agent, so the
  global (non-agent-scoped) upstream path targets the right agent
  without header plumbing (same as the channel proxy).
- **Addressing.** Upstream is dialed as
  `http://{containerPrefix}{name}:{AGENTTEAMS_CONSOLE_PORT}` (default
  `8088`, system-wins env resolution — the same chain the container is
  created with).
- **Standalone workers** (not a member of any team) hide as `404` for
  scoped callers — they have no team to scope to.

## QwenPaw version contract

The proxy is **version-agnostic**: it forwards to fixed, prefixed paths
(`/api/chats…`) and contains no version logic. The version gate is by
pass-through: an upstream 4xx is returned verbatim, so clients can
distinguish "this worker build predates the route" from a real failure
and hide the feature accordingly (the skills proxy's pattern).

The three routes were verified directly against the official PyPI
release wheels (hash-checked):

| QwenPaw release | `GET /api/chats` | `GET /api/chats/{id}` | `GET /api/chats/{id}/status` |
|---|---|---|---|
| 2.0.1 (2026-07-24) | present (`user_id` / `channel` / `archived` filters) | present | **absent** → upstream `404` passed through |
| 2.2.0 (2026-09-03) | present | present | **absent** → upstream `404` passed through |
| 2.2.1 (2026-09-11) | present (`+include_app_owned`) | present (`+include_app_owned`) | present — always `200 {status}` (queries the run tracker, not chat persistence) |

So list + detail work unchanged on any 2.0.1 → 2.2.1 build; the status
route is a 2.2.1+ feature that older builds surface as their own `404`
`{"detail":"Not Found"}`, which dashboards should treat as
"status not available on this worker build" (hide the indicator), not
as an error. `include_app_owned` forwarded to older builds is silently
ignored by their router (unknown query parameters are not rejected).
