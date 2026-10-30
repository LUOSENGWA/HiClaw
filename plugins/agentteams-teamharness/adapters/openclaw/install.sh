#!/usr/bin/env bash
set -euo pipefail

# TeamHarness OpenClaw adapter — install.
#
# Projects the bundle contract onto the OpenClaw runtime, statically:
#
#   1. mcp.servers.teamharness  -> the MCP server entry in openclaw.json
#      (command: python3 -m mcp.server, cwd: <bundle root>, env carries a
#      ${TEAMHARNESS_RUNTIME_CONFIG} reference — the variable name, never a
#      value; OpenClaw expands ${VAR} over the whole parsed config).
#   2. plugins.allow / plugins.load.paths / plugins.entries -> registers this
#      plugin directory (same shape the repo's openclaw-cms-plugin uses).
#      The adapter directory itself is the staged plugin root: the pinned
#      runtime discovers it through plugins.load.paths and loads its index.mjs
#      entry (see that file for the discovery contract). install.sh
#      pre-checks the entry's presence but never copies or generates plugin
#      files — the manifest and the entry ship with the bundle.
#   3. openclaw.plugin.json "skills" -> synced to the bundle's actual skill
#      roots (skills/<root>/<skill>/SKILL.md), relative to the plugin root;
#      an empty array when the bundle ships no skills.
#
# Idempotent: canonical serialization (sorted keys, 2-space indent) means two
# consecutive runs produce byte-identical output; the file is only rewritten
# when the bytes would change. --dry-run prints the resulting configuration
# on stdout and changes nothing. Uninstall removes exactly the keys written
# here (see uninstall.sh); it never removes bundle source files.

usage() {
  cat <<'EOF'
Usage: install.sh [--config <path>] [--workspace <path>] [--dry-run]

  --config <path>  OpenClaw config file (default: $OPENCLAW_CONFIG or
                   $HOME/.openclaw/openclaw.json)
  --workspace <path>
                   OpenClaw workspace directory for isolated runs (created if
                   absent). The plugin registration is absolute-path based
                   (plugins.load.paths) and does not depend on it.
  --dry-run        Print the resulting configuration on stdout; change nothing
EOF
}

CONFIG=""
WORKSPACE=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="${2:?--config requires a value}"; shift 2 ;;
    --workspace) WORKSPACE="${2:?--workspace requires a value}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export AGENTTEAMS_BUNDLE_DIR="${AGENTTEAMS_BUNDLE_DIR:-$BUNDLE_DIR}"

if [ ! -f "${BUNDLE_DIR}/contract.yaml" ]; then
  echo "ERROR: bundle contract not found at ${BUNDLE_DIR}/contract.yaml" >&2
  exit 1
fi
if [ ! -f "${BUNDLE_DIR}/mcp/server.py" ]; then
  echo "ERROR: MCP server entry not found at ${BUNDLE_DIR}/mcp/server.py" >&2
  exit 1
fi
if [ ! -f "${SCRIPT_DIR}/index.mjs" ]; then
  echo "ERROR: loadable plugin entry not found at ${SCRIPT_DIR}/index.mjs" >&2
  exit 1
fi
if [ -n "$WORKSPACE" ] && [ ! -d "$WORKSPACE" ]; then
  mkdir -p "$WORKSPACE"
fi
[ -n "$CONFIG" ] || CONFIG="${OPENCLAW_CONFIG:-$HOME/.openclaw/openclaw.json}"

export TH_OC_ADAPTER_DIR="$SCRIPT_DIR"
export TH_OC_BUNDLE_DIR="$BUNDLE_DIR"
export TH_OC_CONFIG="$CONFIG"
export TH_OC_DRY_RUN="$DRY_RUN"

python3 - <<'PYEOF'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

adapter_dir = Path(os.environ["TH_OC_ADAPTER_DIR"]).resolve()
bundle_dir = Path(os.environ["TH_OC_BUNDLE_DIR"]).resolve()
config_path = Path(os.environ["TH_OC_CONFIG"]).expanduser()
dry_run = os.environ.get("TH_OC_DRY_RUN") == "1"

PLUGIN_ID = "agentteams-teamharness"
MCP_SERVER_ID = "teamharness"
ENV_VAR = "TEAMHARNESS_RUNTIME_CONFIG"
MANIFEST_NAME = "openclaw.plugin.json"


def dump_json(obj):
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def load_json(path):
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    return json.loads(text)


CREDENTIAL_SUFFIXES = ("token", "secret", "password", "passwd", "apikey", "credentials")
REDACTED = "<redacted>"


def _is_credential_key(key):
    normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
    return normalized.endswith(CREDENTIAL_SUFFIXES)


def redact(document):
    """Deep copy with credential values replaced by REDACTED (previews only)."""
    if isinstance(document, dict):
        out = {}
        for key, value in document.items():
            if _is_credential_key(key) and isinstance(value, str) and value:
                out[key] = REDACTED
            else:
                out[key] = redact(value)
        return out
    if isinstance(document, list):
        return [redact(item) for item in document]
    return document


def write_atomic(path, text):
    """Atomic write; keeps the target's mode when it exists, 0600 for new files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def discover_skill_entries():
    """Skill directories (each containing a SKILL.md) as paths relative to the
    plugin root, sorted. Empty when the bundle ships no skills."""
    roots = bundle_dir / "skills"
    entries = []
    if roots.is_dir():
        for skill_md in roots.glob("*/*/SKILL.md"):
            entries.append("../../" + skill_md.parent.relative_to(bundle_dir).as_posix())
    return sorted(entries)


def project(config):
    # 1. MCP server entry — upstream key: mcp.servers.<name> (McpServerConfig
    #    in higress-group/openclaw src/config/types.mcp.ts).
    mcp_entry = {
        "command": "python3",
        "args": ["-m", "mcp.server"],
        "cwd": str(bundle_dir),
        # Pass-through by reference: OpenClaw expands ${TEAMHARNESS_RUNTIME_CONFIG}
        # from the environment; the value is never written into the config.
        "env": {ENV_VAR: "${" + ENV_VAR + "}"},
    }
    config.setdefault("mcp", {}).setdefault("servers", {})[MCP_SERVER_ID] = mcp_entry

    # 2. Plugin registration (openclaw-cms-plugin precedent in this repo).
    plugins = config.setdefault("plugins", {})
    allow = plugins.setdefault("allow", [])
    if PLUGIN_ID not in allow:
        allow.append(PLUGIN_ID)
    load = plugins.setdefault("load", {})
    paths = load.setdefault("paths", [])
    if str(adapter_dir) not in paths:
        paths.append(str(adapter_dir))
    entries = plugins.setdefault("entries", {})
    entries.setdefault(PLUGIN_ID, {})["enabled"] = True

    # 3. Manifest skills sync — declarative, rewritten only when it drifts
    #    from the bundle's actual skill roots.
    manifest_path = adapter_dir / MANIFEST_NAME
    manifest = load_json(manifest_path)
    skills = discover_skill_entries()
    if manifest.get("skills") != skills:
        manifest["skills"] = skills
        if not dry_run:
            write_atomic(manifest_path, dump_json(manifest))
        sys.stderr.write(
            "openclaw adapter: manifest skills synced to %d skill director%s\n"
            % (len(skills), "y" if len(skills) == 1 else "ies")
        )


config = load_json(config_path)
project(config)
result = dump_json(config)

if dry_run:
    sys.stdout.write(dump_json(redact(config)))
    sys.stderr.write("openclaw adapter: dry-run, no files changed\n")
else:
    if config_path.is_file() and config_path.read_text(encoding="utf-8") == result:
        sys.stderr.write("openclaw adapter: %s already up to date\n" % config_path)
    else:
        write_atomic(config_path, result)
        sys.stderr.write("openclaw adapter: wrote %s\n" % config_path)
PYEOF
