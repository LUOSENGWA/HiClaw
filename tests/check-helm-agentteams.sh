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
copaw_render="$(mktemp)"
upg_render="$(mktemp)"
drift_err="$(mktemp)"
trap 'rm -f "${render}" "${copaw_render}" "${upg_render}" "${drift_err}"' EXIT

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

if ! helm template agentteams "${CHART}" "${COMMON_ARGS[@]}" \
    --set worker.defaultImage.copaw.repository=private.registry.example/agentteams-copaw-worker \
    --set worker.defaultImage.copaw.tag=v1.2.3 > "${copaw_render}"; then
    echo "FAIL: helm template (copaw image values) failed:"
    cat "${copaw_render}" || true
    exit 1
fi

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

# ---------------------------------------------------------------------------
# Legacy CoPaw upgrade path (old-chart install -> new-chart upgrade)
# ---------------------------------------------------------------------------
# Previous chart versions resolved the copaw worker image from chart
# defaults (higress-registry.../agentteams-copaw-worker + the release's
# global image tag), so legacy deployments carry no explicit pin in their
# values. This chart's default repository is empty, so a plain upgrade drops
# the env and empty-spec.image workers would fall back to a built-in image
# this release no longer builds. The upgrade must pin the *resolved* old
# image, tag included (tests/check-copaw-helm-upgrade-gate.sh enforces this
# against live clusters before `helm upgrade`).
# ---------------------------------------------------------------------------
LEGACY_COPAW_IMAGE="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-copaw-worker:v1.2.4"
LEGACY_COPAW_TAG="${LEGACY_COPAW_IMAGE##*:}"
LEGACY_COPAW_REPO="${LEGACY_COPAW_IMAGE%:*}"

if ! helm template agentteams "${CHART}" "${COMMON_ARGS[@]}" \
    --set worker.defaultImage.copaw.repository="${LEGACY_COPAW_REPO}" \
    --set worker.defaultImage.copaw.tag="${LEGACY_COPAW_TAG}" > "${upg_render}"; then
    echo "FAIL: helm template (resolved legacy image pin) failed:"
    cat "${upg_render}" || true
    exit 1
fi

if ! grep -q "name: AGENTTEAMS_COPAW_WORKER_IMAGE" "${upg_render}"; then
    echo "FAIL: upgrade pinning the resolved legacy image does not inject AGENTTEAMS_COPAW_WORKER_IMAGE"
    exit 1
fi

if ! grep -q "value: \"${LEGACY_COPAW_IMAGE}\"" "${upg_render}"; then
    echo "FAIL: upgrade must preserve the resolved legacy image verbatim (tag included):"
    grep -n "AGENTTEAMS_COPAW_WORKER_IMAGE" "${upg_render}" || true
    exit 1
fi

echo "PASS: old-chart resolved CoPaw image (tag included) survives upgrade when pinned"

# A partial pin (repository only) must fail the render: the global image
# tag no longer applies to CoPaw, otherwise an upgrade with a changed
# global tag would select a CoPaw image this release no longer builds.
if helm template agentteams "${CHART}" "${COMMON_ARGS[@]}" \
    --set worker.defaultImage.copaw.repository="${LEGACY_COPAW_REPO}" \
    --set global.imageTag=v9.9.9 > /dev/null 2> "${drift_err}"; then
    echo "FAIL: repository-only copaw pin must not render (global image tag must not select a CoPaw image)"
    exit 1
fi

if ! grep -Fq "worker.defaultImage.copaw.tag is required" "${drift_err}"; then
    echo "FAIL: repository-only copaw pin did not fail with the explicit-tag requirement:"
    cat "${drift_err}" || true
    exit 1
fi

echo "PASS: repository-only CoPaw pin fails the render (no global-tag image drift)"
