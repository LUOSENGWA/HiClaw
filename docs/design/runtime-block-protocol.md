# Optional Runtime Block Protocol (`org.agentteams.run` v1)

**Status**: optional cross-component contract. The dashboard-side parser and
normalizers are shipped in the dashboard repository; adoption by runtimes is
incremental and explicitly opt-in. This section is the controller/runtime-side
reference requested in #1312.

**Source of truth**: the dashboard-side field spec
(`agentteams-dashboard/docs/INTERFACES.md`, section "运行时块协议
(`org.agentteams.run` v1)"; implementation: `src/lib/a2ui/protocol.ts`,
parser: `src/lib/a2ui/parser.ts`). Where this document and the dashboard
implementation disagree, the dashboard implementation wins; the two are kept
aligned via the tracking issue (agentscope-ai/AgentTeams#1312).

## 1. What it is

Runtimes deliver a turn's streamed content to rooms as Matrix messages.
Without structure, the dashboard derives presentation (thinking folds,
tool-call cards, confirmation cards, run sentinels) from body-text
heuristics. This protocol gives a runtime an **opt-in typed alternative**:
attach a structured envelope under the message content key
`org.agentteams.run`. Messages without the key — or with an unknown envelope
version — fall back to the existing heuristics. **Messages are never
dropped**, so adoption is safe to roll out incrementally, per runtime.

## 2. Envelope

Matrix message `content['org.agentteams.run']`:

| Field | Type | Required | Notes |
|---|---|---|---|
| `version` | `"1"` \| `"0"` \| absent | no | `"0"`/absent = legacy lenient shape; `"1"` = normalized shape. Any other value = unknown version → the parser returns `undefined` and the caller falls back to the text heuristics |
| `run_id` | string | no | correlates the messages of one run (reserved in v1) |
| `step_id` | string | no | current step within a run (reserved in v1) |
| `blocks` | Block[] | yes | see below |

## 3. Blocks (`type`-discriminated union)

| `type` | Fields | Meaning |
|---|---|---|
| `text` | `text: string`; `isStreaming?` | visible text fragment |
| `thinking` | `content: string`; `isStreaming?` | reasoning fragment (rendered as a collapsible card) |
| `tool_call` | `payload.tool_name: string`; `payload.arguments: object`; `payload.status: 'pending' \| 'running' \| 'succeeded' \| 'failed'`; optional `result`, `tool_call_id`, `started_at` / `finished_at` (epoch ms) | tool-call card; `tool_call_id` is the stable id for deduplication across revisions/replays |
| `confirmation` | `payload.tool_name: string`; `payload.confirmation_id: string` (required); optional `parameters`, `external_files`, `expires_at` | tool-guard approval card; `confirmation_id` links the approval/deny reply |
| `error` | `payload.kind: 'cancelled' \| 'failed' \| 'quiet'`; `payload.title: string` | run-closing sentinel; any other `kind` value is rejected and the heuristics take over |

## 4. Versioning & fallback semantics

1. `resolveProtocolVersion`: `"1"` → v1; absent / `null` / `"0"` → legacy;
   anything else → unknown (the parser as a whole returns `undefined`).
2. v1 normalization: absent optional fields get safe defaults (e.g. `status`
   defaults to `running`); unknown fields are stripped so the payload stays
   serializable. A `confirmation` block without `confirmation_id`, or an
   `error` block with an invalid `kind`, is rejected — that block falls back
   to the legacy heuristics, so e.g. an approval card remains visible.
3. Unknown block types are skipped silently; an unknown **envelope** version
   falls back wholesale. Forward-compatibility promise: a newer version
   renders under the legacy text heuristics on an older dashboard — nothing
   breaks, nothing is dropped.

## 5. Adoption guide (runtime adapters)

1. Emit **only** what `version: "1"` defines; do not rely on extra fields
   (unknown fields are stripped, so any hidden dependency on them is lost).
2. Start with the block types closest to what the adapter already emits —
   typically `thinking`, `tool_call`, `error` (exactly the shapes the legacy
   heuristics approximate).
3. Keep each message's visible `body` human-readable: it remains the
   fallback rendering and the human audit trail in clients that do not parse
   the envelope.
4. Keep `tool_call_id` stable across revisions of the same call; keep
   `confirmation_id` stable across the request/reply pair.
5. Adoption is per-runtime and per-block-type; there is no all-or-nothing
   requirement.

## References

- Field spec & parser: `agentteams-dashboard/docs/INTERFACES.md`
  (section "运行时块协议 (`org.agentteams.run` v1)"), `src/lib/a2ui/protocol.ts`.
- Tracking & alignment: agentscope-ai/AgentTeams#1312.
