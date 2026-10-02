# Coding-CLI Delegation — Operational Guidance

> Companion guidance for the `coding-cli-management` and `coding-cli` skills.
> Distilled from field operation of delegated coding-CLI sessions (field notes
> published on agentscope-ai/AgentTeams#1340). Content-only — no behavior change,
> no new surface.

## 1. Task-spec discipline

A delegated coding task should be a bounded, self-describing unit. The prompt handed to the CLI should carry:

- **Scope** — one coherent change; prefer a small set of related files over one-line fragments or open-ended epics.
- **Deliverables** — exact target files and expected outputs.
- **Acceptance criteria** — how correctness will be judged, stated so a third party could verify it.
- **Verification steps** — commands the runner should execute itself (build / test / lint), with output included in the report.
- **Stop conditions** — when to stop; include the escape hatch: "if blocked or a step cannot run, skip it and say so in the report."
- **Artifact location** — results and logs land in the shared task directory (`shared/tasks/<task-id>/`); session replies are not always re-readable, so "show me" must mean "write it to a file."
- **Declared intent for long waits** — runner-side policies can gate individual commands (a bare standalone wait was blocked pending declared intent); annotate the intent of long waits inline so they clear the gate.

**Scaffolding scales with the runner.** For small/edge models, fully pre-write the change (near-ready spec, exact anchors). For stronger models, explicit goals + constraints + acceptance + self-verification are enough; over-constraining a strong model can reduce quality.

## 2. Supervision and steering

A delegated session is not fire-and-forget. The supervising side should:

- **Define signals** — completion / failure / blocked / pending-approval — and derive them from structured events or status probes, never from parsing prose.
- **Watch without babysitting** — consume the session's event stream and/or a light status probe; push a notification to the orchestrator on completion or failure instead of having someone watch a terminal. This mirrors the taskflow attention model (`docs/design/task-completion-notification.md`): signals first, sync-then-notify.
- **Steer losslessly** — mid-turn instructions queue; issuing a cancel stops the current turn and the queued instruction resumes. Use this to redirect long runs; do not assume mid-turn messages interrupt.
- **Keep state outside the session** — sessions expire between uses; durable state belongs in the workspace (task records, result files), not in session memory.
- **Resolve pending approvals on cancel** — a client that cancels a turn must resolve any pending permission request as cancelled (ACP semantics).
- **End the turn; let wakes drive** — no held session, no polling: completion, timeout and scheduled wakes each resume the orchestrator as a new turn, and the exchange is auditable in the session record.
- **Claim before acting** — several watchers may observe the same completion; take an occupancy token first (idempotent handling).
- **Size watch windows for worst case** — saturated local runners can queue sessions for ~10 minutes; an undersized window expires before completion (we missed one). Arm watchers before work starts; treat a "no activity observed" alert as a first-class signal; give long runs a deadline with a fallback chain: completion → timeout → scheduled self-wake → human.
- **Scheduled runs isolate or share** — default isolated runs (own per-job session; silent — wakes nobody; right for periodic inspection) vs shared runs (delivered into the target session = a real self-wake; required for fallback wake-ups).

## 3. Approval expectations

- **The routine is automated; the risky pauses.** Keep an allow-pattern (in-workspace edits, read-only inspection, build/test) and a hold-list that is never auto-answered: destructive operations, credential paths, secret reads, service managers, production-bound targets.
- **Strict option echoing** — always answer a permission request with one of the option ids carried by that request.
- **Never auto-select a mode-switching option** (e.g. "allow once and switch to default") — humans only.
- **Audit every decision** — who / when / what / why per answer; this is what lets a reviewer reconstruct an unattended run (the durable audit store behind `GET /api/v1/audit` is a natural home).
- **Unanswered requests stall sessions indefinitely.** Plan the human surface (`approval_level` / attention events) so long runs do not depend on someone being online.

## 4. Preflight and environment checks

Before the first real task:

- **Runner present where the session runs** — install or mount accordingly; verify with a trivial round-trip ("ping") before real work.
- **Auth configured end-to-end** — settings file or environment, including base URL and model; secrets never in desired state (`docs/design/member-runtime-config-contract.md`).
- **Environment hygiene** — if ambient variables break the runner, pin the invocation in a small wrapper and register that as the runner command.
- **Network matrix** — on some links, connections are reset selectively by TLS stack generation; use a current stack or a local relay, and document the finding for the deployment.

## 5. Pitfalls → what to do (field-verified)

| Symptom | What to do |
|---|---|
| Runner binary missing at spawn | install/mount where sessions run; ping before real work |
| Auth errors in sequence (authenticate → missing API key) | configure the full auth surface (base URL + model + key) first |
| Ambient environment breaks the runner | wrap the invocation; pin flags and settings |
| Connections reset on some links | use a modern TLS stack or a local relay |
| "Done" appears instantly | prompts ack asynchronously — derive completion from status/events; detect send failures explicitly |
| First status read is "not active" → false done | require "seen active at least once"; grace-timer never-started runs |
| Sessions expire between uses | keep durable state in files/artifacts |
| Permission-answer route differs between builds | pin the canonical form for the deployed version; treat stale answers as benign |
| An option also flips the global mode | never auto-select it |
| Unattended approval stalls a session | policy-answer with holds; escalate only the risky slice |
| Reviewers stop reading approvals | automate the routine; keep the audit trail as the safety net |
| Mid-turn steering doesn't interrupt | queue + cancel → resume |
| VCS operations denied inside sandboxes | keep git at the orchestrator; no git steps in delegation specs |
| Parallel sessions clobber files | one writer per file; isolate workspaces |
| A bare wait/sleep is blocked by policy | annotate intent on long waits; expect command-level gates even after the task is accepted |
| Completion missed — watch window shorter than queue delay | size windows for worst-case queue + execution (~10 min observed); arm watchers before work starts |
| "No activity observed" alert | treat as a first-class signal — it may be the only closure trigger |
| Watchers race on the same completion | claim an occupancy token before acting (idempotent wake handling) |

## 6. Related surfaces

- Adapter precedent and the natural home for runner installation / hook work: `plugins/teamharness/adapters/claude-code/`.
- Team-level task contract: `plugins/teamharness/skills/team/task-delegation/` and `task-execution/`.
- Completion / attention events: `docs/design/task-completion-notification.md`.
- Secrets rule: `docs/design/member-runtime-config-contract.md`.
- Remote-member role: `plugins/teamharness/prompts/agent/remote-member.md`.
