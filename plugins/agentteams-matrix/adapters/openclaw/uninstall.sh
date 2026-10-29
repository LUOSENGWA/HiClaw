#!/usr/bin/env bash
# Idempotent uninstall for the OpenClaw Matrix channel projection.
#
# Removes exactly the keys the installer writes from channels.matrix and
# nothing else: foreign keys (e.g. the controller's streaming settings)
# and foreign sub-keys of the dm/ and groups/ blocks are always
# preserved, as is the config file itself. When the matrix block ends up
# empty it is dropped, along with the channels key only if it is empty as
# well. Re-running is a no-op.
#
# Like the installer, the uninstaller is contract-driven: it re-derives
# the exact removal set from the bundle contract, so only keys the
# installer actually wrote (under the same contract) are removed.
#
# Usage:
#   uninstall.sh [--config <path>] [--dry-run]
#
#   --config <path>   target config file
#                     (default: $HOME/.openclaw/openclaw.json)
#   --dry-run         print the resulting JSON to stdout, write nothing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export AGENTTEAMS_BUNDLE_DIR="${AGENTTEAMS_BUNDLE_DIR:-$BUNDLE_DIR}"

CONFIG_PATH="${HOME}/.openclaw/openclaw.json"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: uninstall.sh [--config <path>] [--dry-run]

Removes the agentteams-matrix keys from channels.matrix in the OpenClaw
config file (default: $HOME/.openclaw/openclaw.json). Foreign keys and
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

CONTRACT_PATH="${AGENTTEAMS_BUNDLE_DIR}/contract.yaml"
if [ ! -f "${CONTRACT_PATH}" ]; then
  echo "ERROR: bundle contract not found at ${CONTRACT_PATH}; set AGENTTEAMS_BUNDLE_DIR to the agentteams-matrix bundle directory" >&2
  exit 1
fi

python3 - "${CONFIG_PATH}" "${CONTRACT_PATH}" "${DRY_RUN}" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
contract_path = Path(sys.argv[2])
dry_run = sys.argv[3] == "1"

# Removal set, re-derived from the contract exactly as the installer
# derives its write set (see install.sh for the upstream evidence):
#   enabled / accessToken — always written (the latter with
#     --with-credentials; removed defensively either way)
#   homeserver / userId / historyLimit — when declared
#   groupPolicy / groupAllowFrom — when group_allow_from is non-empty
#   autoJoin / autoJoinAllowlist — when rooms is non-empty
#   dm.policy / dm.allowFrom — when allow_from is non-empty (deep)
#   groups.<room>.requireMention — per contract room when require_mention
#     is declared (deep; an entry that empties out is dropped)

if not config_path.is_file():
    print("agentteams-matrix openclaw adapter: nothing to uninstall (no config file)")
    sys.exit(0)

try:
    config = json.loads(config_path.read_text(encoding="utf-8"))
except ValueError as exc:
    print(
        f"ERROR: existing config {config_path} is not valid JSON: {exc}",
        file=sys.stderr,
    )
    sys.exit(1)
if not isinstance(config, dict):
    print(
        f"ERROR: existing config {config_path} must contain a JSON object at the top level",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    import yaml
except ImportError:
    print(
        "ERROR: pyyaml is required to read the bundle contract; install pyyaml>=6.0",
        file=sys.stderr,
    )
    sys.exit(1)
try:
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
except yaml.YAMLError as exc:
    print(f"ERROR: bundle contract {contract_path} is not valid YAML: {exc}", file=sys.stderr)
    sys.exit(1)
if not isinstance(contract, dict):
    print(f"ERROR: bundle contract {contract_path} must be a YAML mapping", file=sys.stderr)
    sys.exit(1)
matrix = contract.get("matrix")
if not isinstance(matrix, dict) or not matrix:
    print(f"ERROR: bundle contract {contract_path} has no 'matrix' section", file=sys.stderr)
    sys.exit(1)

channels = config.get("channels")
matrix_cfg = channels.get("matrix") if isinstance(channels, dict) else None
if not isinstance(matrix_cfg, dict):
    print("agentteams-matrix openclaw adapter: nothing to uninstall")
    sys.exit(0)

flat_owned = {"enabled", "accessToken"}
if matrix.get("homeserver") is not None:
    flat_owned.add("homeserver")
if matrix.get("user_id") is not None:
    flat_owned.add("userId")
if matrix.get("history_limit") is not None:
    flat_owned.add("historyLimit")
if matrix.get("group_allow_from"):
    flat_owned.update({"groupPolicy", "groupAllowFrom"})
if matrix.get("rooms"):
    flat_owned.update({"autoJoin", "autoJoinAllowlist"})

removed = [key for key in matrix_cfg if key in flat_owned]
for key in removed:
    del matrix_cfg[key]

# dm: the installer writes policy and allowFrom together.
dm = matrix_cfg.get("dm")
if isinstance(dm, dict) and matrix.get("allow_from"):
    for key in ("policy", "allowFrom"):
        if key in dm:
            del dm[key]
            removed.append(f"dm.{key}")
    if not dm:
        del matrix_cfg["dm"]

# groups: per contract room, drop the requireMention key the installer
# wrote, then drop the entry it created once it is empty (the installer
# always creates an entry per contract room — with requireMention when
# declared, or as an empty admission marker). The groups key itself is
# dropped when it empties out.
groups = matrix_cfg.get("groups")
if isinstance(groups, dict) and matrix.get("rooms"):
    for room in matrix["rooms"]:
        entry = groups.get(room)
        if not isinstance(entry, dict):
            continue
        if matrix.get("require_mention") is not None and "requireMention" in entry:
            del entry["requireMention"]
            removed.append(f"groups.{room}.requireMention")
        if not entry:
            del groups[room]
            removed.append(f"groups.{room}")
    if not groups:
        del matrix_cfg["groups"]

if not removed:
    print("agentteams-matrix openclaw adapter: nothing to uninstall")
    sys.exit(0)

if not matrix_cfg:
    del channels["matrix"]
    if not channels:
        del config["channels"]

rendered = json.dumps(config, indent=2, sort_keys=True) + "\n"

if dry_run:
    sys.stdout.write(rendered)
    print(
        f"agentteams-matrix openclaw adapter: dry run, would remove "
        f"{len(removed)} key(s) from channels.matrix (target: {config_path})",
        file=sys.stderr,
    )
    sys.exit(0)

tmp = config_path.with_name(config_path.name + ".tmp")
tmp.write_text(rendered, encoding="utf-8")
tmp.replace(config_path)
print(
    f"agentteams-matrix openclaw adapter: removed {len(removed)} key(s) "
    f"from channels.matrix in {config_path}"
)
PY
