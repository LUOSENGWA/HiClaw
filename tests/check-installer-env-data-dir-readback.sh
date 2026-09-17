#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="${ROOT_DIR}/install/agentteams-install.sh"

eval "$(sed -n '/^load_current_params_from_env() {/,/^}/p' "${INSTALLER}")"

if ! type load_current_params_from_env >/dev/null 2>&1; then
    echo "FAIL: could not extract load_current_params_from_env from the installer" >&2
    exit 1
fi

# The installer runs this function under plain `set -e` (no pipefail): a grep
# miss on a field absent from the env file must stay harmless, as in
# production. Match those semantics before exercising the function.
set +o pipefail

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT
env_file="${workdir}/agentteams-manager.env"

cat > "${env_file}" << 'EOF'
AGENTTEAMS_LLM_PROVIDER=deepseek
AGENTTEAMS_DEFAULT_MODEL=deepseek-chat
AGENTTEAMS_WORKSPACE_DIR=/opt/agentteams-workspace
AGENTTEAMS_DATA_DIR=custom-vol
AGENTTEAMS_DASHBOARD=1
EOF

# Simulate a fresh upgrade run: nothing exported, values must come from the
# env file. AGENTTEAMS_DATA_DIR is the field that was historically missing
# from the readback; losing it silently re-creates a fresh data volume.
unset AGENTTEAMS_DATA_DIR AGENTTEAMS_LLM_PROVIDER AGENTTEAMS_DEFAULT_MODEL \
    AGENTTEAMS_WORKSPACE_DIR AGENTTEAMS_DASHBOARD

AGENTTEAMS_ENV_FILE="${env_file}"
load_current_params_from_env

for pair in \
    "AGENTTEAMS_DATA_DIR=custom-vol" \
    "AGENTTEAMS_LLM_PROVIDER=deepseek" \
    "AGENTTEAMS_DEFAULT_MODEL=deepseek-chat" \
    "AGENTTEAMS_WORKSPACE_DIR=/opt/agentteams-workspace" \
    "AGENTTEAMS_DASHBOARD=1"
do
    var="${pair%%=*}"
    expected="${pair#*=}"
    actual="${!var:-}"
    if [ "${actual}" != "${expected}" ]; then
        echo "FAIL: expected ${var} to read back as ${expected}, got '${actual}'" >&2
        exit 1
    fi
done

# An exported value must win over the env file (and must not break the
# function's exit status under the installer's set -e).
AGENTTEAMS_DATA_DIR="exported-vol"
load_current_params_from_env
if [ "${AGENTTEAMS_DATA_DIR}" != "exported-vol" ]; then
    echo "FAIL: exported AGENTTEAMS_DATA_DIR must not be overwritten by the env file" >&2
    exit 1
fi

# Env files written before the DATA_DIR field existed must read back empty
# (the deep defense then derives the volume from the live container).
grep -v '^AGENTTEAMS_DATA_DIR=' "${env_file}" > "${env_file}.old"
unset AGENTTEAMS_DATA_DIR
AGENTTEAMS_ENV_FILE="${env_file}.old"
load_current_params_from_env
if [ -n "${AGENTTEAMS_DATA_DIR:-}" ]; then
    echo "FAIL: expected empty AGENTTEAMS_DATA_DIR when the env file predates the field" >&2
    exit 1
fi

echo "PASS: installer reads AGENTTEAMS_DATA_DIR back from the env file on upgrade"
