#!/usr/bin/env bash
# Idempotent uninstall for the Hermes Matrix channel projection.
#
# Removes exactly the keys the installer writes from the platforms.matrix
# block and nothing else: every other key in the config file (terminal,
# memory, other platforms, ...) is preserved, as is the file itself.
# When the platforms.matrix block ends up empty it is dropped, along with
# the platforms key only if it is empty as well. Re-running is a no-op.
#
# Usage:
#   uninstall.sh [--config <path>] [--dry-run]
#
#   --config <path>   target config file
#                     (default: $HOME/.hermes/config.yaml)
#   --dry-run         print the resulting YAML to stdout, write nothing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export AGENTTEAMS_BUNDLE_DIR="${AGENTTEAMS_BUNDLE_DIR:-$BUNDLE_DIR}"

CONFIG_PATH="${HOME}/.hermes/config.yaml"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: uninstall.sh [--config <path>] [--dry-run]

Removes the agentteams-matrix keys from the platforms.matrix block of the
Hermes config file (default: $HOME/.hermes/config.yaml). Foreign keys and
the config file itself are preserved.
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

python3 - "${CONFIG_PATH}" "${DRY_RUN}" <<'PY'
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
dry_run = sys.argv[2] == "1"


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


try:
    import yaml
except ImportError:
    fail("pyyaml is required to read the config file; install pyyaml>=6.0")

# Exactly the keys the installer can write: enabled, homeserver, user_id,
# allowed_users, allowed_rooms, require_mention, plus the credential key
# that --with-credentials adds (token — the typed PlatformConfig field,
# gateway/config.py:411).
OWNED_KEYS = {
    "enabled",
    "homeserver",
    "user_id",
    "allowed_users",
    "allowed_rooms",
    "require_mention",
    "token",
}


def render(document):
    return yaml.safe_dump(
        document,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


if not config_path.is_file():
    print("agentteams-matrix hermes adapter: nothing to uninstall (no config file)")
    sys.exit(0)

try:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
except yaml.YAMLError as exc:
    fail(f"existing config {config_path} is not valid YAML: {exc}")
if config is None:
    config = {}
if not isinstance(config, dict):
    fail(f"existing config {config_path} must be a YAML mapping at the top level")

platforms = config.get("platforms")
matrix_cfg = platforms.get("matrix") if isinstance(platforms, dict) else None
if not isinstance(matrix_cfg, dict):
    print("agentteams-matrix hermes adapter: nothing to uninstall")
    sys.exit(0)

removed = [key for key in matrix_cfg if key in OWNED_KEYS]
if not removed:
    print("agentteams-matrix hermes adapter: nothing to uninstall")
    sys.exit(0)

for key in removed:
    del matrix_cfg[key]
if not matrix_cfg:
    del platforms["matrix"]
    if not platforms:
        del config["platforms"]

rendered = render(config)

if dry_run:
    sys.stdout.write(rendered)
    print(
        f"agentteams-matrix hermes adapter: dry run, would remove "
        f"{len(removed)} key(s) from the platforms.matrix block (target: {config_path})",
        file=sys.stderr,
    )
    sys.exit(0)

tmp = config_path.with_name(config_path.name + ".tmp")
tmp.write_text(rendered, encoding="utf-8")
tmp.replace(config_path)
print(
    f"agentteams-matrix hermes adapter: removed {len(removed)} key(s) "
    f"from the platforms.matrix block in {config_path}"
)
PY
