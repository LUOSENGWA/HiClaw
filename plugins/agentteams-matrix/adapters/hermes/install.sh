#!/usr/bin/env bash
# Idempotent config projection of the agentteams-matrix bundle contract
# onto the Hermes runtime.
#
# The adapter reads the bundle contract (contract.yaml, matrix: section)
# and merges its settings into the platforms.matrix block of the Hermes
# config file, nested under the block's explicit `extra:` map — the one
# shape both supported runtime generations consume (see README.md,
# "Config shape and runtime versions"). It is a pure file projector: no
# network access is performed; the only runtime interaction is an
# optional `hermes --version` probe when the target version is not
# declared via --hermes-version or HERMES_VERSION.
#
# Usage:
#   install.sh [--config <path>] [--dry-run] [--with-credentials]
#              [--hermes-version <version>] [--allow-partial]
#
#   --config <path>        target config file
#                          (default: $HOME/.hermes/config.yaml)
#   --dry-run              print the resulting YAML to stdout, write nothing
#   --with-credentials     also write the access token read from
#                          $AGENTTEAMS_MATRIX_ACCESS_TOKEN; the config
#                          file is written 0600 whenever it carries
#                          credential values
#   --hermes-version <v>   target hermes-agent version. Overrides the
#                          HERMES_VERSION env var and the `hermes
#                          --version` probe. An undetermined version is
#                          treated as the pre-0.22.5 runtime (safe
#                          default). Calendar tags (v2026.x.y) belong to
#                          the pre-0.x "Hermes Agent" series and count
#                          as pre-0.22.5.
#   --allow-partial        on a pre-0.22.5 runtime, project only the
#                          settings that runtime can honor and record
#                          the dropped ones in
#                          <config dir>/.agentteams-matrix/hermes-install.json
#                          (without it, the installer exits non-zero when
#                          the contract requires settings that runtime
#                          would silently ignore)
#
# Version support: pre-0.22.5 runtimes (including the Worker pin
# v2026.4.16 = "Hermes Agent v0.10.0") consume enabled/token (typed) and
# extra.{homeserver, user_id, device_id, password}; the user/room
# allowlists and require_mention do not exist there (require_mention is
# env-only, MATRIX_REQUIRE_MENTION). The full contract therefore
# requires >= 0.22.5, and the installer never exits 0 while dropping
# contract settings.
#
# Only the keys this adapter owns are merged into the platforms.matrix
# block; every other key in the config file is preserved, including
# foreign keys already present inside the block or inside its existing
# extra map. Re-running with an unchanged contract produces a
# byte-identical file.
# Previews (--dry-run) always redact credential values in the rendered
# document: any non-empty string under a key ending in token/secret/
# password/passwd/apikey/credentials (case-insensitive) is printed as
# <redacted>, including credentials in blocks this adapter does not own;
# the file itself is written unredacted. The atomic write never loosens
# permissions: a document that carries credential values lands at 0600,
# and an existing restrictive mode survives every rewrite. By default no
# credential material is written at all (the contract's
# access_token_ref has no Hermes key — see README.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export AGENTTEAMS_BUNDLE_DIR="${AGENTTEAMS_BUNDLE_DIR:-$BUNDLE_DIR}"

CONFIG_PATH="${HOME}/.hermes/config.yaml"
DRY_RUN=0
WITH_CREDENTIALS=0
ALLOW_PARTIAL=0
HERMES_VERSION_ARG=""

usage() {
  cat <<'EOF'
Usage: install.sh [--config <path>] [--dry-run] [--with-credentials]
                  [--hermes-version <version>] [--allow-partial]

Projects the agentteams-matrix contract onto the platforms.matrix block
of the Hermes config file (default: $HOME/.hermes/config.yaml).

Settings are written into the block's explicit `extra:` map, which both
runtime generations consume. Pre-0.22.5 runtimes (including the Worker
pin v2026.4.16) cannot honor allowed_users / allowed_rooms /
require_mention; when the contract requires them the installer exits
non-zero unless --allow-partial is passed, in which case the dropped
settings are recorded in
<config dir>/.agentteams-matrix/hermes-install.json.
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
    --hermes-version)
      if [ $# -lt 2 ]; then
        echo "ERROR: --hermes-version requires a value" >&2
        exit 2
      fi
      HERMES_VERSION_ARG="$2"
      shift 2
      ;;
    --allow-partial)
      ALLOW_PARTIAL=1
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

python3 - "${CONFIG_PATH}" "${CONTRACT_PATH}" "${DRY_RUN}" "${WITH_CREDENTIALS}" "${ALLOW_PARTIAL}" "${HERMES_VERSION_ARG}" <<'PY'
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

config_path = Path(sys.argv[1])
contract_path = Path(sys.argv[2])
dry_run = sys.argv[3] == "1"
with_credentials = sys.argv[4] == "1"
allow_partial = sys.argv[5] == "1"
version_arg = sys.argv[6]

TOKEN_KEY = "token"
MARKER_DIR_NAME = ".agentteams-matrix"
MARKER_NAME = "hermes-install.json"
FULL_CONTRACT_MINIMUM = (0, 22, 5)
# Contract settings a pre-0.22.5 runtime cannot honor from the config
# file: the user/room allowlists do not exist in that generation and
# require_mention is env-only (MATRIX_REQUIRE_MENTION).
UNHONORABLE_SETTINGS = ("allowed_users", "allowed_rooms", "require_mention")

CREDENTIAL_SUFFIXES = ("token", "secret", "password", "passwd", "apikey", "credentials")
REDACTED = "<redacted>"


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


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


def _document_has_credentials(document):
    if isinstance(document, dict):
        return any(
            (_is_credential_key(key) and isinstance(value, str) and value)
            or _document_has_credentials(value)
            for key, value in document.items()
        )
    if isinstance(document, list):
        return any(_document_has_credentials(item) for item in document)
    return False


def write_atomic(path, text, document):
    """Atomic write. 0600 whenever the document carries credentials, otherwise
    preserve the target's mode (0600 for a new file). The temp file is created
    0600 by mkstemp and only then chmod-ed to the final mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if _document_has_credentials(document):
        mode = 0o600
    elif path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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

# contract.yaml -> config.yaml platforms.matrix block. The settings are
# projected into the block's explicit `extra:` map — the one shape both
# runtime generations consume:
#
#   * pre-0.22.5 (pinned v2026.4.16): PlatformConfig.from_dict reads
#     extra = data.get("extra", {}) and ignores every other non-typed
#     top-level key — flat settings are silently dropped (#1458);
#   * >= 0.22.5: PlatformConfig.from_dict promotes non-typed top-level
#     keys into extra, but an explicit `extra:` map wins on a clash, so
#     the nested shape is authoritative there too.
#
# Flat keys are therefore NOT written (see README.md, "Config shape and
# runtime versions").
projected_extra = {}
# homeserver — consumed via config.extra on both generations.
if matrix.get("homeserver") is not None:
    projected_extra["homeserver"] = matrix["homeserver"]
# user_id — consumed via config.extra on both generations.
if matrix.get("user_id") is not None:
    projected_extra["user_id"] = matrix["user_id"]
# allow_from + group_allow_from -> allowed_users: the adapter has a
# single user allowlist for DMs and groups, so the two contract lists
# are merged (deduped, contract order preserved). YAML lists are
# accepted by the adapter's CSV-set reader.
allowed_users = list(
    dict.fromkeys(list(matrix.get("allow_from") or []) + list(matrix.get("group_allow_from") or []))
)
if allowed_users:
    projected_extra["allowed_users"] = allowed_users
# rooms -> allowed_rooms — room-ID gate, DMs exempt (>= 0.22.5 only).
rooms = list(matrix.get("rooms") or [])
if rooms:
    projected_extra["allowed_rooms"] = rooms
# require_mention — declared value projected as-is (>= 0.22.5 only).
if matrix.get("require_mention") is not None:
    projected_extra["require_mention"] = bool(matrix["require_mention"])

# NOT projected (no upstream key — see README.md):
#   access_token_ref — Hermes credentials are the typed token field
#     or the MATRIX_ACCESS_TOKEN env var; there is no secret-reference key.
#   history_limit / history_mode — no equivalent in the matrix adapter.

access_token = None
if with_credentials:
    access_token = os.environ.get("AGENTTEAMS_MATRIX_ACCESS_TOKEN", "")
    if not access_token:
        fail(
            "--with-credentials requires the AGENTTEAMS_MATRIX_ACCESS_TOKEN "
            "environment variable to be set"
        )


# --- Runtime version gate -------------------------------------------------
# Resolution order: --hermes-version -> HERMES_VERSION env ->
# `hermes --version` -> unknown. Unknown is treated as the pre-0.22.5
# runtime (safe default): a config whose required settings the runtime
# would silently ignore must not be written as a success.

def parse_version(raw):
    """First numeric dotted run of *raw* as an int tuple, else None.

    A leading component >= 1000 is a calendar tag (v2026.4.16 = the
    pre-0.x "Hermes Agent v0.10.0" series), which predates 0.22.5."""
    if not raw:
        return None
    match = re.search(r"\d+(?:\.\d+)+", raw)
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def is_new_runtime(version):
    parsed = parse_version(version)
    if parsed is None:
        return False
    if parsed[0] >= 1000:
        return False
    padded = parsed + (0,) * max(0, 3 - len(parsed))
    return padded[:3] >= FULL_CONTRACT_MINIMUM


def detect_version():
    if version_arg:
        return version_arg.strip()
    env_version = os.environ.get("HERMES_VERSION", "").strip()
    if env_version:
        return env_version
    try:
        proc = subprocess.run(
            ["hermes", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()


marker_path = config_path.parent / MARKER_DIR_NAME / MARKER_NAME
required = [key for key in UNHONORABLE_SETTINGS if key in projected_extra]
runtime_version = detect_version()
dropped = []
if not is_new_runtime(runtime_version) and required:
    label = runtime_version or "unknown"
    if not allow_partial:
        fail(
            f"hermes runtime {label} (pre-0.22.5) cannot honor these "
            f"contract settings: {', '.join(required)}. The installer "
            "refuses to write a config that would silently ignore "
            "required settings. Target a runtime >= 0.22.5 "
            "(--hermes-version / HERMES_VERSION) or pass --allow-partial "
            f"to project the remaining settings and record the dropped "
            f"ones in {marker_path}."
        )
    dropped = required
    for key in required:
        del projected_extra[key]
    print(
        f"WARNING: hermes runtime {label} (pre-0.22.5) cannot honor "
        f"these contract settings; they were not written: "
        f"{', '.join(required)} (partial projection recorded in "
        f"{marker_path})",
        file=sys.stderr,
    )

# --- Merge into the target config ------------------------------------------

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

block = dict(matrix_cfg)
# enabled — typed PlatformConfig field (both generations).
block["enabled"] = True
# token — typed PlatformConfig field, written only with --with-credentials.
if access_token:
    block[TOKEN_KEY] = access_token
# Merge into the block's existing extra map; foreign extra keys survive.
existing_extra = block.get("extra")
if existing_extra is not None and not isinstance(existing_extra, dict):
    fail(f"'platforms.matrix.extra' in {config_path} is not a YAML mapping")
if existing_extra is None:
    existing_extra = {}
block["extra"] = {**existing_extra, **projected_extra}
platforms["matrix"] = block
config["platforms"] = platforms

if dry_run:
    sys.stdout.write(render(redact(config)))
    print(
        f"agentteams-matrix hermes adapter: dry run, no files written "
        f"(target: {config_path})",
        file=sys.stderr,
    )
    sys.exit(0)

write_atomic(config_path, render(config), config)

marker_dir = marker_path.parent
if dropped:
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = {
        "adapter": "hermes",
        "bundle": "agentteams-matrix",
        "config": str(config_path),
        "dropped": sorted(dropped),
        "runtime_version": runtime_version or "unknown",
    }
    write_atomic(
        marker_path,
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        marker,
    )
    print(
        f"agentteams-matrix hermes adapter: partial projection into "
        f"platforms.matrix in {config_path} (runtime "
        f"{runtime_version or 'unknown'}; dropped {', '.join(sorted(dropped))})"
    )
else:
    # A full projection supersedes any stale partial-projection marker.
    if marker_path.is_file():
        marker_path.unlink()
        try:
            marker_dir.rmdir()
        except OSError:
            pass
    print(
        f"agentteams-matrix hermes adapter: wrote platforms.matrix to "
        f"{config_path} (runtime {runtime_version or 'unknown'}; full projection)"
    )
PY
