#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART="${ROOT_DIR}/helm/agentteams"
COMMON_ARGS=(
    --set credentials.registrationToken=test
    --set credentials.adminPassword=test
    --set credentials.llmApiKey=test
    --set gateway.publicURL=http://localhost:18080
)

render="$(mktemp)"
trap 'rm -f "${render}"' EXIT

helm template agentteams "${CHART}" "${COMMON_ARGS[@]}" > "${render}"

grep -q 'name: agentteams-controller' "${render}"
grep -q 'app.kubernetes.io/name: agentteams' "${render}"

echo "PASS: AgentTeams Helm release renders canonical resource names"

# ---------------------------------------------------------------------------
# Legacy CoPaw worker image (issue #1310 / PR #1335)
# ---------------------------------------------------------------------------
# New releases must NOT inject AGENTTEAMS_COPAW_WORKER_IMAGE (the CoPaw
# worker image is gone from fresh installs). Existing CoPaw deployments
# keep their image only by explicitly pinning worker.defaultImage.copaw.*
# in values, in which case the pinned repository:tag must be rendered into
# the controller env so workers with an empty spec.image keep pulling it.
# ---------------------------------------------------------------------------

if grep -q "AGENTTEAMS_COPAW_WORKER_IMAGE" "${render}"; then
    echo "FAIL: default values must not inject AGENTTEAMS_COPAW_WORKER_IMAGE"
    grep -n "AGENTTEAMS_COPAW_WORKER_IMAGE" "${render}" || true
    exit 1
fi

copaw_render="$(helm template agentteams "${CHART}" "${COMMON_ARGS[@]}" \
    --set worker.defaultImage.copaw.repository=private.registry.example/agentteams-copaw-worker \
    --set worker.defaultImage.copaw.tag=v1.2.3 2>&1)" || {
    echo "FAIL: helm template (copaw image values) failed:"
    echo "${copaw_render}"
    exit 1
}

if ! grep -q "name: AGENTTEAMS_COPAW_WORKER_IMAGE" "${copaw_render}"; then
    echo "FAIL: explicitly pinned copaw image is not injected as AGENTTEAMS_COPAW_WORKER_IMAGE"
    grep -n "AGENTTEAMS_COPAW_WORKER_IMAGE" "${copaw_render}" || true
    exit 1
fi

if ! grep -q "value: \"private.registry.example/agentteams-copaw-worker:v1.2.3\"" "${copaw_render}"; then
    echo "FAIL: AGENTTEAMS_COPAW_WORKER_IMAGE value not rendered from pinned repository:tag"
    grep -n "AGENTTEAMS_COPAW_WORKER_IMAGE" "${copaw_render}" || true
    exit 1
fi

echo "PASS: AgentTeams Helm release keeps legacy CoPaw worker image opt-in (no default injection)"
