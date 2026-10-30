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
#                        $AGENTTEAMS_MATRIX_ACCESS_TOKEN; the config
#                        file is written 0600 whenever it carries
#                        credential values
#
# Only the keys this adapter owns are merged into the platforms.matrix
# block; every other key in the config file is preserved, including
# foreign keys already present inside the block. Re-running with an
# unchanged contract produces a byte-identical file.
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
import stat
import sys
import tempfile
from pathlib import Path

config_path = Path(sys.argv[1])
contract_path = Path(sys.argv[2])
dry_run = sys.argv[3] == "1"
with_credentials = sys.argv[4] == "1"

TOKEN_KEY = "token"

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
    sys.stdout.write(render(redact(config)))
    print(
        f"agentteams-matrix hermes adapter: dry run, no files written "
        f"(target: {config_path})",
        file=sys.stderr,
    )
    sys.exit(0)

write_atomic(config_path, render(config), config)
print(
    f"agentteams-matrix hermes adapter: wrote {len(projected)} key(s) to "
    f"platforms.matrix in {config_path}"
)
PY
