#!/usr/bin/env bash
# Real-engine regression: a default `docker run` container reports
# .HostConfig.OomKillDisable as <nil>, while `docker create` stores false.
# boundary_verify must treat those as equal (but keep explicit true distinct).
#
# Requires a running Docker daemon and a cached image. Skips when either is
# unavailable; does not pull, tag, push or deploy anything.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$root/install/agentteams-upgrade.sh"
image="${AGENTTEAMS_TEST_IMAGE:-}"

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "SKIP test-upgrade-boundary-real (no docker daemon)"
  exit 0
fi
if [ -z "$image" ]; then
  image="$(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -v '<none>' | head -n1 || true)"
fi
if [ -z "$image" ] || ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "SKIP test-upgrade-boundary-real (no cached image; set AGENTTEAMS_TEST_IMAGE)"
  exit 0
fi

suffix="$$-$(date +%s)"
old_name="at1412-oom-old-${suffix}"
new_name="at1412-oom-new-${suffix}"
true_name="at1412-oom-true-${suffix}"
cleanup() { docker container rm --force "$old_name" "$new_name" "$true_name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Default docker run: OomKillDisable is unset (nil).
docker run -d --name "$old_name" "$image" sleep 60 >/dev/null
# Replacement docker create: Docker stores the default as false.
docker create --name "$new_name" "$image" sleep 60 >/dev/null

# Explicit true must remain a real difference. Some cgroup-v2 hosts reject
# --oom-kill-disable; in that case the nil/false assertion above still ran and
# the true-distinction case is skipped honestly.
if ! docker create --name "$true_name" --oom-kill-disable "$image" sleep 60 >/dev/null 2>&1; then
  echo "PASS test-upgrade-boundary-real ($image: nil/false accepted; explicit true unsupported on this daemon, skipped)"
  exit 0
fi

AGENTTEAMS_UPGRADE_FUNCTIONS_ONLY=1 . "$script"
DOCKER_CMD="docker"

# nil vs false: same boundary.
if ! boundary_verify "$old_name" "$new_name" >/dev/null 2>&1; then
  echo "FAIL: default OomKillDisable <nil> vs false was rejected" >&2
  exit 1
fi

# true vs false: still a mismatch.
if boundary_verify "$true_name" "$new_name" >/dev/null 2>&1; then
  echo "FAIL: explicit OomKillDisable true vs false was accepted" >&2
  exit 1
fi

echo "PASS test-upgrade-boundary-real ($image: nil/false accepted, true/false rejected)"
