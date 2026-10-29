# TeamHarness OpenClaw Adapter

Projects the bundle contract (`../../contract.yaml`) onto the OpenClaw
runtime, statically. After `install.sh`, the TeamHarness MCP server is
declared in the OpenClaw runtime configuration and this plugin directory is
registered; no runtime code executes at install time.

## What `install.sh` writes

Target: `~/.openclaw/openclaw.json` (override with `--config <path>` or
`OPENCLAW_CONFIG`).

| Key | Value |
|-----|-------|
| `mcp.servers.teamharness` | `{command: python3, args: ["-m", "mcp.server"], cwd: <bundle root>, env: {TEAMHARNESS_RUNTIME_CONFIG: "${TEAMHARNESS_RUNTIME_CONFIG}"}}` |
| `plugins.allow` | `+ "agentteams-teamharness"` (deduplicated) |
| `plugins.load.paths` | `+ <this adapter directory>` (deduplicated) |
| `plugins.entries.agentteams-teamharness.enabled` | `true` |
| `openclaw.plugin.json` `skills` | synced to the bundle's actual skill roots (`skills/<root>/<skill>`, each a directory containing `SKILL.md`), relative to this plugin directory; `[]` when the bundle ships no skills |

The MCP entry is a *static projection*: `python3 -m mcp.server` with
`cwd` = bundle root (the `mcp/` package has no `__init__.py`, so the module
path resolves from the bundle root). `TEAMHARNESS_RUNTIME_CONFIG` is written
as a `${VAR}` reference — the variable name, never a value. OpenClaw expands
`${VAR}` over the whole parsed configuration; an unset variable degrades to a
warning and keeps the placeholder, it does not crash the worker.

## Usage

```sh
install.sh                    # merge into ~/.openclaw/openclaw.json
install.sh --dry-run          # print resulting config on stdout, change nothing
install.sh --config /path     # target a different config file
uninstall.sh                  # remove exactly the keys above, keep everything else
uninstall.sh --dry-run
```

Both scripts are idempotent: canonical serialization (sorted keys, 2-space
indent) makes two consecutive runs byte-identical, and files are only
rewritten when the bytes would change. A freshly created config file is
written `0600`; an existing file's mode is preserved. No credentials are ever
written.

## Upstream key provenance (higress-group/openclaw @ 2f35b6fa, pinned in `openclaw-base/Dockerfile`)

- `mcp.servers.<name>` — `src/config/types.mcp.ts` (`McpServerConfig`:
  `command`, `args`, `env`, `cwd`, `workingDirectory`, `url`, `transport`,
  `headers`, ...). This is the upstream-equivalent of the task's
  `mcpServers` shape; there is **no top-level `mcpServers` key** in
  openclaw.json.
- `~/.openclaw/openclaw.json` — `docs/gateway/configuration-reference.md`.
- `${VAR}` substitution — `src/config/env-substitution.ts` (recursive over
  the parsed config, uppercase names, `$${` escape; missing variable warns
  and keeps the placeholder).
- Manifest format — `docs/plugins/manifest.md`: `openclaw.plugin.json`
  requires `id` + `configSchema`; `skills: string[]` are "skill directories
  to load, relative to the plugin root". Only documented manifest fields are
  read; **there is no `mcpServers` manifest key** — MCP servers are runtime
  configuration, declared in openclaw.json instead.
- Plugin registration keys (`plugins.allow` / `plugins.load.paths` /
  `plugins.entries`) — same shape the repo already uses for the
  openclaw-cms-plugin (`manager/scripts/init/start-manager-agent.sh`,
  `manager/agent/skills/worker-management/scripts/generate-worker-config.sh`).

## Deviations & limitations (disclosed)

1. **No `mcpServers` in the manifest.** The task's wording suggested an
   `mcpServers` key on `openclaw.plugin.json`; upstream ignores undocumented
   manifest keys ("Avoid adding custom top-level keys here" —
   `docs/plugins/manifest.md`). The MCP server is therefore declared where
   OpenClaw actually reads MCP declarations: `mcp.servers` in openclaw.json.
2. **Manifest-only plugin directory.** This directory ships
   `openclaw.plugin.json` (valid upstream shape: `id` + `configSchema` +
   `skills`) but no JavaScript entry. `detectBundleManifestFormat`
   (`src/plugins/bundle-manifest.ts`) treats a directory with
   `openclaw.plugin.json` as a standard plugin (not a codex/cursor/claude
   bundle); whether discovery activates a plugin that has a manifest but no
   entry module is not documented at the pinned commit. The MCP server entry
   does not depend on plugin loading — it is plain runtime configuration and
   is effective regardless. Verify plugin activation against the pinned
   build when the runtime E2E (non-goal here) is run.
3. **`skills` paths use `../../`** (plugin root is `adapters/openclaw`,
   skills live at the bundle root). OpenClaw's path handling for out-of-root
   skill directories is not documented at the pinned commit; install.sh
   keeps the manifest in sync with the bundle so no fabricated entries
   exist.
4. **`commands.mcp` is not set** — it only enables the chat `/mcp` command;
   server availability does not depend on it.

## Lifecycle

- **Install** — run `install.sh`; safe to re-run (byte-identical).
- **Upgrade** — update the bundle, re-run `install.sh`; the MCP entry and
  manifest skills are re-projected, plugin registration is a no-op.
- **Uninstall** — `uninstall.sh` removes only this adapter's keys; other
  entries in openclaw.json are preserved. The bundle files themselves are
  not touched.
- **Reload** — the OpenClaw worker picks up the configuration on its next
  start.
