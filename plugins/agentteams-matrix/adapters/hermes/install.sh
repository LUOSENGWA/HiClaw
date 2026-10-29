#!/usr/bin/env bash
# Idempotent config projection of the agentteams-matrix bundle contract
# onto the Hermes runtime.
#
# The adapter reads the bundle contract (contract.yaml, matrix: section)
# and merges its keys into the platforms.matrix block of the Hermes
# config file. It is a pure file projector: no hermes binary is invoked
# and no network access is performed.
#
# Usage:
#   install.sh [--config <path>] [--dry-run] [--with-credentials]
#
#   --config <path>      target config file
#                        (default: $HOME/.hermes/config.yaml)
#   --dry-run            print the resulting YAML to stdout, write nothing
#   --with-credentials   also write the access token read from
#                        $AGENTTEAMS_MATRIX_ACCESS_TOKEN and chmod 600
#                        the target file
#
# Only the keys this adapter owns are merged into the platforms.matrix
# block; every other key in the config file is preserved, including
# foreign keys already present inside the block. Re-running with an
# unchanged contract produces a byte-identical file. The access token is
# never printed: by default no credential material is written at all
# (the contract's access_token_ref has no Hermes key — see README.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export AGENTTEAMS_BUNDLE_DIR="${AGENTTEAMS_BUNDLE_DIR:-$BUNDLE_DIR}"

CONFIG_PATH="${HOME}/.hermes/config.yaml"
DRY_RUN=0
WITH_CREDENTIALS=0

usage() {
  cat <<'EOF'
Usage: install.sh [--config <path>] [--dry-run] [--with-credentials]

Projects the agentteams-matrix contract onto the platforms.matrix block
of the Hermes config file (default: $HOME/.hermes/config.yaml).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --config)
      if [ $# -lt 2 ]; then
        echo "ERROR: --config requires a path" >&2
        exit 2
      fi
      CONFIG_PATH="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --with-credentials)
      WITH_CREDENTIALS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

CONTRACT_PATH="${AGENTTEAMS_BUNDLE_DIR}/contract.yaml"
if [ ! -f "${CONTRACT_PATH}" ]; then
  echo "ERROR: bundle contract not found at ${CONTRACT_PATH}; set AGENTTEAMS_BUNDLE_DIR to the agentteams-matrix bundle directory" >&2
  exit 1
fi

python3 - "${CONFIG_PATH}" "${CONTRACT_PATH}" "${DRY_RUN}" "${WITH_CREDENTIALS}" <<'PY'
import os
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
contract_path = Path(sys.argv[2])
dry_run = sys.argv[3] == "1"
with_credentials = sys.argv[4] == "1"

TOKEN_KEY = "token"


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


try:
    import yaml
except ImportError:
    fail("pyyaml is required to read the bundle contract; install pyyaml>=6.0")


def render(document):
    return yaml.safe_dump(
        document,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


try:
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
except yaml.YAMLError as exc:
    fail(f"bundle contract {contract_path} is not valid YAML: {exc}")
if not isinstance(contract, dict):
    fail(f"bundle contract {contract_path} must be a YAML mapping")
matrix = contract.get("matrix")
if not isinstance(matrix, dict) or not matrix:
    fail(f"bundle contract {contract_path} has no 'matrix' section")

# contract.yaml -> config.yaml platforms.matrix block. The block is
# parsed by PlatformConfig.from_dict (upstream Hermes, gateway/config.py:
# 445), which promotes every non-typed top-level key into config.extra
# (config.py:448-452); the matrix adapter then reads its settings
# extra-first (plugins/platforms/matrix/adapter.py, see README.md for
# the per-key evidence).
#
# enabled — typed field (config.py:410,467).
projected = {"enabled": True}
# homeserver — non-typed, promoted to config.extra; read at adapter.py:829.
if matrix.get("homeserver") is not None:
    projected["homeserver"] = matrix["homeserver"]
# user_id — non-typed, promoted to config.extra; read at adapter.py:831.
if matrix.get("user_id") is not None:
    projected["user_id"] = matrix["user_id"]
# allow_from + group_allow_from -> allowed_users: the adapter has a single
# user allowlist for DMs and groups (adapter.py:898, applied at
# adapter.py:2611-2612), so the two contract lists are merged (deduped,
# contract order preserved). YAML lists are accepted by _csv_set
# (adapter.py:546-548).
allowed_users = list(
    dict.fromkeys(list(matrix.get("allow_from") or []) + list(matrix.get("group_allow_from") or []))
)
if allowed_users:
    projected["allowed_users"] = allowed_users
# rooms -> allowed_rooms — non-typed, promoted to config.extra; read at
# adapter.py:863 (DMs are exempt from the room gate, adapter.py:1973-1981).
rooms = list(matrix.get("rooms") or [])
if rooms:
    projected["allowed_rooms"] = rooms
# require_mention — non-typed, promoted to config.extra; read at
# adapter.py:940 (default true; declared value is projected as-is).
if matrix.get("require_mention") is not None:
    projected["require_mention"] = bool(matrix["require_mention"])

# NOT projected (no upstream key — see README.md):
#   access_token_ref — Hermes credentials are the typed token field
#     (config.py:411,468; read at adapter.py:830) or the
#     MATRIX_ACCESS_TOKEN env var; there is no secret-reference key.
#   history_limit / history_mode — no equivalent in the matrix adapter.

access_token = None
if with_credentials:
    access_token = os.environ.get("AGENTTEAMS_MATRIX_ACCESS_TOKEN", "")
    if not access_token:
        fail(
            "--with-credentials requires the AGENTTEAMS_MATRIX_ACCESS_TOKEN "
            "environment variable to be set"
        )
if access_token:
    projected[TOKEN_KEY] = access_token

config = None
if config_path.is_file():
    raw = config_path.read_text(encoding="utf-8")
    if raw.strip():
        try:
            config = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            fail(f"existing config {config_path} is not valid YAML: {exc}")
if config is None:
    config = {}
if not isinstance(config, dict):
    fail(f"existing config {config_path} must be a YAML mapping at the top level")

platforms = config.get("platforms")
if platforms is None:
    platforms = {}
if not isinstance(platforms, dict):
    fail(f"'platforms' in {config_path} is not a YAML mapping")
matrix_cfg = platforms.get("matrix")
if matrix_cfg is None:
    matrix_cfg = {}
if not isinstance(matrix_cfg, dict):
    fail(f"'platforms.matrix' in {config_path} is not a YAML mapping")

merged = {**matrix_cfg, **projected}
platforms["matrix"] = merged
config["platforms"] = platforms

if dry_run:
    if access_token:
        shown = dict(config)
        shown["platforms"] = dict(config["platforms"])
        shown["platforms"]["matrix"] = {
            **config["platforms"]["matrix"],
            TOKEN_KEY: "<redacted>",
        }
        sys.stdout.write(render(shown))
    else:
        sys.stdout.write(render(config))
    print(
        f"agentteams-matrix hermes adapter: dry run, no files written "
        f"(target: {config_path})",
        file=sys.stderr,
    )
    sys.exit(0)

config_path.parent.mkdir(parents=True, exist_ok=True)
tmp = config_path.with_name(config_path.name + ".tmp")
tmp.write_text(render(config), encoding="utf-8")
tmp.replace(config_path)
if access_token:
    os.chmod(config_path, 0o600)
print(
    f"agentteams-matrix hermes adapter: wrote {len(projected)} key(s) to "
    f"platforms.matrix in {config_path}"
)
PY
