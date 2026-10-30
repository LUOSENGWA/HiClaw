# TeamHarness OpenClaw Adapter

Projects the bundle contract (`../../contract.yaml`) onto the OpenClaw
runtime, statically. After `install.sh`, the TeamHarness MCP server is
declared in the OpenClaw runtime configuration and this plugin directory is
registered as a loadable plugin; no runtime code executes at install time.

This directory **is** the OpenClaw plugin root (registered via
`plugins.load.paths`):

- `index.mjs` — the loadable plugin entry that native discovery loads (see
  below).
- `openclaw.plugin.json` — plugin manifest (id, name, version, configSchema,
  skills).
- `install.sh` / `uninstall.sh` — black-box scripts that project the runtime
  configuration and manage the plugin registration, respectively.

### Loadable entry (`index.mjs`)

At the pinned release, plugin discovery (`src/plugins/discovery.ts`) adds a
directory as a plugin candidate only when it carries a supported bundle
manifest (`.codex-plugin/`, `.cursor-plugin/`, `.claude-plugin/`, or
`plugin.json`) **or** a default entry file — `index.ts` / `index.js` /
`index.mjs` / `index.cjs` (`DEFAULT_PLUGIN_ENTRY_CANDIDATES`,
`src/plugins/manifest.ts`). A directory holding only `openclaw.plugin.json`
is skipped (`detectBundleManifestFormat` returns null for it), which is why
`index.mjs` exists: it makes this directory discoverable. The loader unwraps
the module's default export (`src/plugins/loader.ts`,
`unwrapDefaultModuleExport`), so the plugin is a plain default-exported
object. The plugin is declarative — it imports nothing from the `openclaw`
package and registers nothing at runtime; the TeamHarness functionality is
the `mcp.servers.teamharness` entry and the manifest skill roots, both
projected by `install.sh`.

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

`install.sh` also pre-checks that `index.mjs` exists in this directory and
fails fast when the bundle is incomplete. `--workspace <path>` is accepted
for isolated runs (the directory is created if absent); the registration is
absolute-path based and does not depend on the workspace.

The MCP entry is a *static projection*: `python3 -m mcp.server` with
`cwd` = bundle root (the `mcp/` package has no `__init__.py`, so the module
path resolves from the bundle root). `TEAMHARNESS_RUNTIME_CONFIG` is written
as a `${VAR}` reference — the variable name, never a value. OpenClaw expands
`${VAR}` over the whole parsed configuration; an unset variable degrades to a
warning and keeps the placeholder, it does not crash the worker.

## Usage

```sh
install.sh                              # merge into ~/.openclaw/openclaw.json
install.sh --dry-run                    # print resulting config on stdout, change nothing
install.sh --config /path               # target a different config file
install.sh --config /path --workspace /tmp/ws   # isolated run (workspace created if absent)
uninstall.sh                            # remove exactly the keys above, keep everything else
uninstall.sh --dry-run
```

Both scripts are idempotent: canonical serialization (sorted keys, 2-space
indent) makes two consecutive runs byte-identical, and files are only
rewritten when the bytes would change. A freshly created config file is
written `0600`; an existing file's mode is preserved. No credentials are ever
written. Dry-run previews redact existing credential values (string values
under keys ending in `token`, `secret`, `password`, `passwd`, `apikey`, or
`credentials`) as `<redacted>`; the written file itself is never redacted.

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
2. **Plugin entry, not a bundle manifest.** This directory ships
   `index.mjs` (a default-exported plugin object) rather than a
   `.codex-plugin/`/`.cursor-plugin/`/`.claude-plugin/` bundle manifest.
   That is the loadable route at the pinned commit: discovery
   (`src/plugins/discovery.ts`) requires a supported bundle manifest or a
   default entry file (`DEFAULT_PLUGIN_ENTRY_CANDIDATES`), and
   `openclaw.plugin.json` alone makes a directory a non-candidate
   (`detectBundleManifestFormat` returns null for it). The loader unwraps
   the default export (`src/plugins/loader.ts`,
   `unwrapDefaultModuleExport`); both the directory scan and
   `plugins.load.paths` resolve this same directory, so both register the
   same plugin id.
3. **`skills` paths use `../../`** (plugin root is `adapters/openclaw`,
   skills live at the bundle root). OpenClaw's path handling for out-of-root
   skill directories is not documented at the pinned commit; install.sh
   keeps the manifest in sync with the bundle so no fabricated entries
   exist.
4. **`commands.mcp` is not set** — it only enables the chat `/mcp` command;
   server availability does not depend on it.

## Native discovery acceptance (pinned runtime)

Reproduce the maintainer's P1 check on the pinned release
(2026.4.14 / `2f35b6fa`), offline:

```sh
export HOME=<scratch-home>          # empty directory; no network
bash install.sh --config $HOME/.openclaw/openclaw.json --workspace <tmp>/ws
openclaw plugins list --json        # agentteams-teamharness present, no
                                    # "plugin not found" warnings
bash uninstall.sh --config $HOME/.openclaw/openclaw.json
openclaw plugins list --json        # id gone, no warnings return
```

The regression drives these steps against the real CLI when `OPENCLAW_BIN`
is available (or `openclaw` is on PATH) and is wired into CI
(`.github/workflows/test-controller.yml`, `openclaw-native-discovery` job,
`npm install -g openclaw@2026.4.14`); it skips with a clear reason
otherwise.

## Lifecycle

- **Install** — run `install.sh`; safe to re-run (byte-identical).
- **Upgrade** — update the bundle, re-run `install.sh`; the MCP entry and
  manifest skills are re-projected, plugin registration is a no-op.
- **Uninstall** — `uninstall.sh` removes only this adapter's configuration
  keys (MCP entry, `plugins.allow` / `plugins.load.paths` /
  `plugins.entries`); other entries in openclaw.json are preserved. Unlike
  the Hermes adapter (which stages a copy into `~/.hermes/plugins/`), this
  adapter registers its own directory in place, so there are no staged
  copies to remove — the bundle source files (`index.mjs`,
  `openclaw.plugin.json`) are never touched.
- **Reload** — the OpenClaw worker picks up the configuration on its next
  start.
