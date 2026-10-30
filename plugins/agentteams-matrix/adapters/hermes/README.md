# agentteams-matrix — Hermes Adapter

This adapter projects the bundle contract (`../../contract.yaml`) onto the
Hermes runtime by merging its `matrix:` section into the `platforms.matrix`
block of the Hermes config file. It is a pure file projector: it reads the
contract, rewrites one config file, and nothing else — no `hermes` binary
is invoked and no network access is performed.

Key names and the target block follow the upstream Hermes gateway config
(v0.22.5, commit bf0203a7):

- The `platforms:` top-level key of `~/.hermes/config.yaml` is parsed by
  `GatewayConfig.from_dict`
  (`gateway/config.py:781` → `by_platform("platforms",
  PlatformConfig.from_dict, dicts_only=True)`).
- `PlatformConfig.from_dict` promotes **every non-typed top-level key of a
  platform block into `config.extra`** (`gateway/config.py:445`,
  promotion at `config.py:448-452`, typed-key list at `config.py:439-442`).
  This is where `config.extra` lands on disk: as plain keys directly under
  `platforms.matrix:` in the YAML.
- The matrix adapter reads its settings extra-first:
  `config.extra.get(key)` → `MATRIX_*` env → default
  (`gateway/platforms/_shared.py:106`, `plugins/platforms/matrix/adapter.py`).
- The AgentTeams worker bridge uses the same split: credentials flow through
  `MATRIX_*` env, `platforms.matrix.enabled` is set in `config.yaml`
  (`hermes/src/hermes_worker/bridge.py`, `bridge_openclaw_to_hermes`).

## Key mapping

| Contract field (`matrix:`) | Hermes key (`platforms.matrix.`) | How it is consumed (file:line) |
|:--|:--|:--|
| — (constant `true`) | `enabled` | typed `PlatformConfig` field: `gateway/config.py:410,467` |
| `homeserver` | `homeserver` | non-typed → `config.extra`; read at `plugins/platforms/matrix/adapter.py:829` (fallback env `MATRIX_HOMESERVER`) |
| `user_id` | `user_id` | non-typed → `config.extra`; read at `adapter.py:831` (fallback env `MATRIX_USER_ID`) |
| `allow_from[]` + `group_allow_from[]` | `allowed_users` (union, deduped, contract order) | the adapter has a **single** user allowlist for DMs and groups (no DM/group distinction): `adapter.py:898`, applied at `adapter.py:2611-2612` (fallback env `MATRIX_ALLOWED_USERS`); YAML lists are accepted by `_csv_set` (`adapter.py:546-548`) |
| `rooms[]` | `allowed_rooms` | room-ID gate, DMs exempt: `adapter.py:863`, `adapter.py:1973-1981` (fallback env `MATRIX_ALLOWED_ROOMS`) |
| `require_mention` | `require_mention` | non-typed → `config.extra`; read at `adapter.py:940` (fallback env `MATRIX_REQUIRE_MENTION`, default `true`) |
| `access_token_ref` | — (not projected) | no secret-reference concept: credentials are the typed `token` field (`gateway/config.py:411,468`, read at `adapter.py:830`) or the `MATRIX_ACCESS_TOKEN` env var |
| `history_limit` | — (not projected) | no equivalent key in the matrix adapter (zero matches) |
| `history_mode` | — (not projected) | no equivalent key in the matrix adapter (zero matches) |

## Install

```bash
plugins/agentteams-matrix/scripts/install.sh --adapter hermes
# or directly:
adapters/hermes/install.sh [--config <path>] [--dry-run] [--with-credentials]
```

The default config path is `$HOME/.hermes/config.yaml` (override with
`--config`; `~/.hermes` is the platform default home,
`hermes_constants.py:51-56`). `--dry-run` prints the resulting YAML to
stdout and writes nothing.

The installer merges only the keys it owns into the `platforms.matrix`
block and preserves every other key in the file (`terminal`, `memory`,
other platforms, ...), including foreign keys already present inside the
block (e.g. `reply_to_mode`). Re-running with an unchanged contract
produces a byte-identical file.

## Credentials

By default the installer writes **no credential material at all**: the
contract's `access_token_ref` has no Hermes key (see the table above),
and nothing is printed to stdout. With `--with-credentials` it writes the
typed `token` field from the `AGENTTEAMS_MATRIX_ACCESS_TOKEN` environment
variable (hard error if the variable is unset). The config file is
written at mode 0600 whenever the rendered document carries a credential
value — written now or already present in the file — and the atomic
rewrite never loosens an existing mode, so a config that still holds a
credential after an uninstall stays 0600.

`--dry-run` previews redact credential values in the rendered document:
any non-empty string under a key ending in `token`, `secret`, `password`,
`passwd`, `apikey` or `credentials` (case-insensitive) is printed as
`<redacted>` — including credentials in sibling blocks this adapter does
not own. The config file itself is always written unredacted.

Where the env path is the established credential channel (as in the
worker bridge), the env var `MATRIX_ACCESS_TOKEN` wins over the YAML
token (`adapter.py:830`).

## Uninstall

```bash
adapters/hermes/uninstall.sh [--config <path>] [--dry-run]
```

Removes exactly the keys the installer can write (`enabled`, `homeserver`,
`user_id`, `allowed_users`, `allowed_rooms`, `require_mention`, `token`)
from the `platforms.matrix` block, keeps all foreign keys, and never
deletes the config file. When the block ends up empty it is dropped, and
the `platforms` key only if it is empty as well. Without anything to
remove the uninstall is a no-op, so re-running is safe.

## Requirements

bash, python3 with pyyaml (to read the contract and the config). No
runtime binary, no network.
