# Skill Catalog API

Status: implemented
API: `GET /api/v1/skills`

## Problem

The workbench (and any API client) needs a browsable catalog of the skills a
team can assign to its workers: the built-in skills shipped with the agent
templates, and the remote registry skills (e.g. Nacos) already referenced in
the cluster. Before this endpoint there was no way to list them through the
controller API — the Dashboard Skill Center reads its own private storage,
which user-facing clients cannot reach, and the Worker CRD only records what
is already assigned, not what is available.

## Design

A single read-only endpoint, `GET /api/v1/skills`, served by a new
`SkillsHandler`:

1. **Builtin skills.** The handler scans the same agent-template
   directories the deployer uses when provisioning workers
   (`Deployer.builtinAgentDir`: the default worker template, the `copaw`
   and `hermes` runtime variants, and the team-leader template). Each
   `skills/<skill>/SKILL.md` frontmatter contributes `name` and
   `description`; the directory name is the fallback name. The same skill
   shipped by several templates is reported once, with the providing
   templates listed in `agents`.
2. **Remote skills.** Every Worker's `spec.remoteSkills` is aggregated: each
   referenced skill name appears with its registry `source`. A remote skill
   that is also builtin is reported once with a combined source
   (`builtin+nacos`) so clients can show both availability paths.
3. **No content access.** Only frontmatter metadata is read. Skill bodies
   and registry credentials are never exposed; the endpoint performs no
   network calls (remote skills are enumerated from the Worker specs, not
   from the registry).

## Contract

`GET /api/v1/skills` → `200`

```json
{
  "skills": [
    {"name": "file-sync", "description": "Sync files with centralized storage.", "source": "builtin+nacos", "agents": ["copaw-worker-agent", "worker-agent"]},
    {"name": "web-research", "source": "nacos"}
  ],
  "total": 2
}
```

- `source` is `"builtin"`, a registry source such as `"nacos"`, or a
  `+`-joined combination when both apply.
- `agents` is present for builtin skills (sorted, deduplicated).
- Output is sorted by `name`; missing template directories (deployment
  without some runtimes) are silently skipped.
- Errors: none expected; a backend read failure degrades to the builtin
  list. No `4xx` paths.

## Authorization

`ActionList` on the new `skills` resource kind. The catalog is
metadata-only (skill names/descriptions, no PII, no credentials), so it is
available to admins, managers, team leaders, and team-scoped humans; worker
service accounts are denied. No scope filtering — availability is
deployment-wide, and per-worker assignment remains a separate (write)
concern.

## Out of scope

- Registry-side listing (querying Nacos for skills no worker references yet).
- Skill content download / upload (Dashboard Skill Center territory).
- Per-team filtering (the catalog is global metadata; assignment is
  per-worker).

## Tests

- `internal/server/skills_handler_test.go` — builtin scan across template
  dirs with dedup (`file-sync` in two templates → one entry, two `agents`),
  remote aggregation (pure-remote and builtin+remote combined sources),
  stray non-directory files ignored, missing template dir → empty 200,
  empty `WorkerAgentDir` → empty 200, sorted output, frontmatter parser
  cases (valid / no frontmatter / name-only / empty / missing file).
