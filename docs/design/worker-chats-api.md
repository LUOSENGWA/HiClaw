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
worker, enforces the worker-scoped read boundary **and the participation
boundary** (an L2 human sees only their own conversations with the
worker — never the worker's conversations with an admin, another user,
or an app), and forwards to the worker's qwenpaw app over the shared
docker network. The dashboard renders the results as the worker's
session list / agent context / run status.

**Read-only by design.** There is no create / archive / delete / stream
route: the conversation lifecycle stays on the qwenpaw app and its own
channels.

## Routes

| Route | Upstream (worker qwenpaw app) | Returns |
|-------|------------------------------|---------|
| `GET /api/v1/workers/{name}/chats` | `GET /api/chats` | `list[ChatSpec]` — id, name, user_id, channel, created/updated, pinned, archived, … (L2 humans: only their own chats) |
| `GET /api/v1/workers/{name}/chats/{chat_id}` | `GET /api/chats/{chat_id}` | `ChatHistory{messages: [Message], status}` — the saved **agent context** converted to messages (L2 humans: own chats only) |
| `GET /api/v1/workers/{name}/chats/{chat_id}/status` | `GET /api/chats/{chat_id}/status` | `ChatStatusResponse{status: "idle" \| "running"}` (QwenPaw ≥ 2.2.1; L2 humans: own chats only) |

- `{chat_id}` is the qwenpaw chat id — always a lowercase UUIDv4 (every
  chat is created with `str(uuid4())`). Anything else is rejected with
  `400` before the dial (no path injection possible).
- The list endpoint forwards the documented read-only filters
  `?user_id=`, `?channel=`, `?archived=`, `?include_app_owned=`
  (whitelist; unknown parameters → `400`, never `422`). For L2 humans
  the `user_id` filter is **overridden server-side** to the caller's own
  Matrix MXID (a client-supplied value is a filter, never
  authorization) and `include_app_owned` is dropped — see
  *Participation boundary*. The detail and status routes take no query
  parameters.
- 2xx responses stream verbatim (a context can be large; no body cap,
  same as the checkpoint proxy). Upstream 4xx responses pass through
  verbatim (see *QwenPaw version contract*). Upstream 5xx is wrapped in
  a `502` with a truncated body.

### Agent context versus chat history

The detail route returns the worker's **saved agent context** converted
to messages — not a guaranteed complete transcript of what was exchanged
in the channel. The context may have been compacted and may contain
tool output that was never sent to the channel. For Matrix
conversations, the messages actually exchanged in the room remain
authoritative in Matrix (subject to Matrix membership /
history-visibility rules); this proxy is a context-inspection surface,
not a second source of room history. The dashboard should label the
view "Agent context", not "Chat history".

## Authorization

### Worker scope (layer 1)

The routes ride on the standard worker `GET` authorization (L1 admin:
any worker; L2 human / team leader: own-team workers via the
`TeamMatches` scope check; standalone humans: `404`). Cross-team access
returns `404`, uniformly with "no such worker" — worker existence cannot
be probed (404-not-403, same as the other worker-scoped reads).
Embedded mode only: kube mode returns `503` uniformly.

### Participation boundary (layer 2 — L2 humans)

Passing the worker scope is necessary but not sufficient for an L2
human: they may converse with the worker, but they may only **view the
conversations they participate in** — not the worker's private
conversations with an admin, another user, or a PawApp. The boundary is
enforced server-side on all three routes:

- **Anchor.** An L2 human authenticates AS their Matrix user, so
  "participation in a matrix conversation" is anchored on the caller's
  Matrix MXID — the Human CR's `status.matrixUserID`, the
  reconciler-verified id the Matrix authenticator cross-checks against
  the token's whoami. Matrix chats store the sender's MXID as the chat's
  `user_id`, so a chat is the caller's iff `user_id` equals the anchor.
  Non-matrix channels (qq/console/cron) store other id namespaces, so
  no L2 human can claim those chats — they are excluded by the same
  server-side `user_id` filter, no per-channel allowlist needed.
- **List.** The `user_id` filter is forced to the anchor server-side
  (a client-supplied `user_id` is overridden — a filter, never
  authorization) and `include_app_owned` is dropped: it is a content
  filter for PawApp-owned chats, which are never a human's
  conversations. `channel` / `archived` still narrow the caller's own
  set.
- **Detail / status.** A participation precheck (the upstream list
  scoped to the anchor must contain the chat id) runs before the dial;
  an absent chat returns a **uniform 404 in the upstream's own
  not-found shape** (`{"detail":"Chat not found: {id}"}`) —
  indistinguishable from a genuinely missing chat, so neither other
  users' chat existence nor content can be probed, and the upstream
  detail endpoint is never dialed for a denied request. A precheck
  upstream failure returns `502`, not a false 404: participation that
  cannot be proven must not silently hide a healthy worker.
- **Fail closed.** If the Human CR cannot be resolved or has no
  reconciled `status.matrixUserID`, the whole surface hides for that
  caller (uniform `404`, no upstream dial) — an unprovable anchor must
  not leak a conversation view.
- **L3 (worker-scoped) humans** have no chats access in v1 — consistent
  with #1277, which deliberately keeps the other read surfaces
  (checkpoints, skills, …) team-scoped and lists extensions as
  follow-ups. Their `WorkerReadable` leg does not apply to this route.

Full-view callers (L1 admin, manager SA, team leader SA) skip layer 2
entirely — they see the complete list (with client filters) and dial
detail/status directly.

**Data sensitivity.** An agent context is the worker's saved
conversation context and may contain tool output or fragments of stored
configuration. Access is bounded by worker scope AND participation —
no new credential or capability is introduced, and callers outside the
team cannot even confirm the worker exists.

## Status mapping

| Upstream / situation | Controller response |
|----------------------|---------------------|
| worker name / chat id fails validation | `400` |
| worker not found | `404` `{"message":"worker not found"}` |
| scoped caller outside the worker's team | `404` (same body as unknown worker) |
| L3 (worker-scoped) human | `404` (no chats access in v1) |
| L2 human, Human CR unresolvable / no reconciled matrixUserID | `404` (uniform, fail closed, no upstream dial) |
| L2 human, chat not in their own list (detail/status) | `404` `{"detail":"Chat not found: {id}"}` (upstream's own shape) |
| L2 human, precheck upstream call fails | `502` (participation unprovable is not a 404) |
| kube mode | `503` |
| upstream unreachable (5 s timeout) | `502` `{"message":"worker unreachable"}` |
| upstream 2xx | `200`, body + `Content-Type` verbatim |
| upstream 4xx | passed through verbatim (status + body) |
| upstream 5xx | `502`, body truncated to 128 bytes |

## Example

```bash
# L2 human (alice) lists HER sessions with a worker in her team —
# the server forces user_id to alice's own MXID; she never sees the
# worker's conversations with anyone else.
curl -s http://127.0.0.1:8090/api/v1/workers/daily-carol/chats \
  -H "Authorization: Bearer $AGENTTEAMS_MATRIX_TOKEN"
# → 200 [{"id":"0d9f…","name":"matrix:…","user_id":"@alice:…","channel":"matrix",…}, …]

# narrow her own sessions by channel
curl -s "http://127.0.0.1:8090/api/v1/workers/daily-carol/chats?channel=matrix" \
  -H "Authorization: Bearer $AGENTTEAMS_MATRIX_TOKEN"

# open the agent context of one of HER sessions
curl -s http://127.0.0.1:8090/api/v1/workers/daily-carol/chats/0d9f3d6e-…-0e1f \
  -H "Authorization: Bearer $AGENTTEAMS_MATRIX_TOKEN"
# → 200 {"messages":[{"type":"message","role":"user","content":[…],…}], "status":"idle"}

# a chat that is not hers → uniform 404, indistinguishable from absent
curl -s http://127.0.0.1:8090/api/v1/workers/daily-carol/chats/{other-user-chat-id} \
  -H "Authorization: Bearer $AGENTTEAMS_MATRIX_TOKEN"
# → 404 {"detail":"Chat not found: {other-user-chat-id}"}

# is the worker currently replying in that session? (QwenPaw ≥ 2.2.1)
curl -s http://127.0.0.1:8090/api/v1/workers/daily-carol/chats/0d9f3d6e-…-0e1f/status \
  -H "Authorization: Bearer $AGENTTEAMS_MATRIX_TOKEN"
# → 200 {"status":"running"}   (always 200 on 2.2.1+; 404 on older builds)

# L1 admin sees the full list (client filters apply as written)
curl -s "http://127.0.0.1:8090/api/v1/workers/daily-carol/chats?user_id=@bob:…" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
# → 200 [ …all of the worker's chats matching the filter… ]
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

## Tests (`internal/server/worker_chats_test.go`)

- Full-view passthrough: `TestChatList_ForwardsVerbatim`,
  `TestChatList_AllWhitelistedParamsForwarded`,
  `TestChatDetail_ForwardsVerbatim`, `TestChatStatus_Forwards` — admin
  queries forwarded (sorted), bodies verbatim, large bodies streamed.
- **Participation boundary:**
  - `TestChat_L2HumanInScopeAllowed` — in-scope L2 human resolves 200
    with the list **server-forced** to her own MXID
    (`user_id=%40alice%3Aexample.com` upstream).
  - `TestChat_L2Participation_ListOverridesClientFilters` — a
    client-supplied `user_id=bob` is overridden to alice's MXID,
    `include_app_owned` dropped, `channel`/`archived` kept.
  - `TestChat_L2Participation_OtherUsersChatDetail404` — bob's chat
    404s in the upstream's own shape; the upstream detail endpoint is
    **never dialed** (no content, no existence probe).
  - `TestChat_L2Participation_OwnChatDetail200` — alice's own chat
    passes the precheck and streams verbatim.
  - `TestChat_L2Participation_StatusSameBoundary` — the status route
    runs the same precheck (other 404 / own 200).
  - `TestChat_L2Participation_HumanCRUnresolved404` — no Human CR, or
    an empty `status.matrixUserID`: uniform 404, **zero** upstream
    dials (fail closed).
  - `TestChat_L2Participation_PreachUpstreamFailure502` — a failing
    precheck list call 502s (unprovable participation ≠ 404).
  - `TestChat_L3HumanDenied` — L3 (worker-scoped) human: 404, upstream
    never dialed (v1: no chats access, #1277 posture).
- Scope layer: `TestChat_TeamLeaderCrossTeamDenied`,
  `TestChat_StandaloneHumanDenied` (both unchanged — 404, no probe).
- Version gate: `TestChatStatus_Upstream404IsTheVersionGate` (older
  builds' 404 passed through verbatim).
- Robustness: validation 400s, kube-mode 503, unknown worker 404,
  bounded 502 bodies, unreachable-worker 502, prefix/port resolution.

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
