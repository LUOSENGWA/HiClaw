"""Orchestrator contract tests for ``upgrade_qwenpaw_runtime.sh``.

The heavy path matrix lives in the script's own ``--selftest`` (15
scenarios, fully stubbed); this file re-runs that gate as a pytest test
and adds targeted two-phase scenarios the selftest does not cover:
boot-check SUCCESS (version == target) and the pre-restart install-fail
rollback (no restart, pending cleaned, state rolled_back).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]  # .../AgentTeams-runtime-upgrade/qwenpaw
SCRIPT = REPO_ROOT / "scripts" / "upgrade_qwenpaw_runtime.sh"


def run_bash(script_text: str, env: dict | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", script_text],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=timeout,
    )


def _stub_env(d: Path, **extra) -> dict:
    base = {
        # version probe: read from a plain file the scenarios control
        "UG_VERSION_CMD": f"cat {d}/version",
        # skip the real pre-flight checker (no network / no wheel in tests)
        "UG_CHECKER_CMD": "true",
    }
    base.update(extra)
    return base


def test_orchestrator_selftest_all_green():
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--selftest"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SELFTEST: 15/15 PASS" in proc.stdout


def test_boot_check_success_retains_pending_for_post_start(tmp_path):
    d = tmp_path
    (d / "version").write_text("2.2.2\n")  # venv already reports the target
    pending = d / "pending.json"
    pending.write_text(json.dumps({
        "job_id": "upg-20261012-000000-abcd",
        "old": "2.2.1",
        "target": "2.2.2",
        "venv": str(d / "venv"),
    }))
    state = d / "state.json"

    proc = run_bash(
        f"bash {SCRIPT} --phase boot-check --venv {d}/venv "
        f"--pending-file {pending} --state-file {state}",
        env=_stub_env(d),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUMMARY: status=boot-check-passed phase=boot-check target=2.2.2" in proc.stdout
    # The app is not up yet at boot-check time: pending is RETAINED and the
    # state is the intermediate boot-check-passed (not success), so the
    # entrypoint's post-start health gate remains the arbiter and a package
    # that installed fine but whose app fails to start still rolls back.
    assert pending.exists()
    data = json.loads(state.read_text())
    assert data["state"] == "boot-check-passed"


def test_post_start_success_removes_pending(tmp_path):
    d = tmp_path
    (d / "version").write_text("2.2.2\n")  # running app serves the target
    pending = d / "pending.json"
    pending.write_text(json.dumps({
        "job_id": "upg-20261012-000000-abcd",
        "old": "2.2.1",
        "target": "2.2.2",
        "venv": str(d / "venv"),
    }))
    state = d / "state.json"
    proc = run_bash(
        f"bash {SCRIPT} --phase post-start --venv {d}/venv "
        f"--pending-file {pending} --state-file {state} --timeout 5 --settle 0",
        env=_stub_env(d, UG_ALIVE_CMD="echo running"),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUMMARY: status=ok phase=post-start target=2.2.2" in proc.stdout
    assert not pending.exists()
    data = json.loads(state.read_text())
    assert data["state"] == "success"


def test_post_start_fail_rolls_back(tmp_path):
    d = tmp_path
    (d / "version").write_text("2.2.2\n")
    calls = d / "calls"
    pending = d / "pending.json"
    pending.write_text(json.dumps({
        "job_id": "upg-20261012-000000-ffff",
        "old": "2.2.1",
        "target": "2.2.2",
        "venv": str(d / "venv"),
    }))
    state = d / "state.json"
    env = _stub_env(
        d,
        # App failed to start: the version probe (which in-container hits the
        # running app's /api/version, not pip show) cannot read a version, so
        # the health gate times out. This is the shiyiyue scenario — a
        # correctly installed package (venv reports the target at boot-check)
        # whose app then fails to come up.
        UG_VERSION_CMD="exit 1",
        UG_ALIVE_CMD="echo not-alive",
        UG_INSTALL_CMD=f'echo install-{{target}} >> {calls}; true',
        UG_PATCH_CMD=f'echo patch-{{target}} >> {calls}; true',
        UG_RESTART_CMD=f'echo restart >> {calls}; true',
    )
    proc = run_bash(
        f"bash {SCRIPT} --phase post-start --venv {d}/venv "
        f"--pending-file {pending} --state-file {state} --timeout 2 --settle 0",
        env=env,
    )
    # New contract (P1-2): the rollback install/patch/restart all succeed,
    # but the post-rollback health gate (supervisorctl mode re-verifies the
    # restored old runtime IN-PROCESS) cannot pass with a dead version probe.
    # That is a FAILED recovery, not a success: loud FATAL + exit 4 + job
    # state rollback_failed + pending retained for manual retry.
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "FATAL: ROLLBACK FAILED" in proc.stdout + proc.stderr
    got = calls.read_text().splitlines()
    assert "install-2.2.1" in got  # rollback reinstalled the old version
    assert "patch-2.2.1" in got
    assert "restart" in got        # post-start rollback restarts (app was up)
    assert pending.exists()        # kept: the recovery is NOT verified
    data = json.loads(state.read_text())
    assert data["state"] == "rollback_failed"


def test_pre_restart_install_fail_rolls_back_without_restart(tmp_path):
    d = tmp_path
    (d / "version").write_text("2.2.1\n")
    calls = d / "calls"
    pending = d / "pending.json"
    state = d / "state.json"
    env = _stub_env(d,
        # fails for the target (2.2.2), succeeds when the rollback
        # reinstalls the old version (2.2.1)
        UG_INSTALL_CMD=f'echo install-{{target}} >> {calls}; [ "{{target}}" = "2.2.1" ]',
        UG_PATCH_CMD=f'echo patch-{{target}} >> {calls}; true',
        UG_RESTART_CMD=f'echo restart >> {calls}; true',
    )

    proc = run_bash(
        f"bash {SCRIPT} --phase pre-restart --target 2.2.2 "
        f"--job-id upg-20261012-000000-ffff --venv {d}/venv "
        f"--pending-file {pending} --state-file {state}",
        env=env,
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    got = calls.read_text().splitlines()
    assert "install-2.2.2" in got          # the failed target install
    assert "install-2.2.1" in got          # the rollback reinstall
    assert "patch-2.2.1" in got            # patch re-applied for old
    assert "patch-2.2.2" not in got        # never reached for the target
    assert "restart" not in got            # app never died -> no restart
    assert not pending.exists()            # cleaned after rollback
    data = json.loads(state.read_text())
    assert data["state"] == "rolled_back"
    assert "SUMMARY: status=rolled_back" in proc.stdout


def test_pre_restart_requires_job_id():
    proc = run_bash(f"bash {SCRIPT} --phase pre-restart --target 2.2.2")
    assert proc.returncode == 2
    assert "--job-id" in proc.stderr


# ---------------------------------------------------------------------------
# P1-1 (shiyiyue #1364 review): post-start completion and concurrency
#
# The entrypoint invokes boot-check/post-start with ONLY --venv and
# --pending-file (no --job-id / --state-file). The job context must therefore
# travel in pending.json (state_file) so that post-start can close the job
# state (success / rolled_back) instead of leaving it stranded at
# boot-check-passed, and so runner.active_job() keeps treating the job as
# active (409) across the whole two-phase window.
# ---------------------------------------------------------------------------

def test_pending_carries_job_context_from_pre_restart(tmp_path):
    d = tmp_path
    (d / "version").write_text("2.2.1\n")
    calls = d / "calls"
    pending = d / "pending.json"
    state = d / "state.json"
    env = _stub_env(d,
        UG_INSTALL_CMD=f'echo install-{{target}} >> {calls}; true',
        UG_PATCH_CMD=f'echo patch-{{target}} >> {calls}; true',
        UG_RESTART_CMD=f'echo restart >> {calls}; true',
    )
    proc = run_bash(
        f"bash {SCRIPT} --phase pre-restart --target 2.2.2 "
        f"--job-id upg-20261014-000000-bbbb --venv {d}/venv "
        f"--pending-file {pending} --state-file {state}",
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(pending.read_text())
    assert data["job_id"] == "upg-20261014-000000-bbbb"
    # phase 2 runs entrypoint-style (--pending-file only) and must be able to
    # find the job's state file from pending alone:
    assert data["state_file"] == str(state)


def test_post_start_closes_job_state_from_pending_alone(tmp_path):
    """entrypoint-style post-start (no --job-id/--state-file): on a passed
    health gate the job must close to ``success`` in its state file."""
    d = tmp_path
    (d / "version").write_text("2.2.2\n")  # the running app serves the target
    up = d / "upgrade"
    up.mkdir()
    job = "upg-20261014-000000-aaaa"
    state = up / f"upgrade-{job}.json"
    state.write_text(json.dumps({
        "job_id": job,
        "state": "boot-check-passed",  # boot-check left it here (pending retained)
        "current": "2.2.1",
        "target": "2.2.2",
        "started_at": "2026-10-14T00:00:00Z",
    }))
    pending = up / "pending.json"
    pending.write_text(json.dumps({
        "job_id": job,
        "old": "2.2.1",
        "target": "2.2.2",
        "venv": str(d / "venv"),
        "state_file": str(state),
    }))
    proc = run_bash(
        f"bash {SCRIPT} --phase post-start --venv {d}/venv "
        f"--pending-file {pending} --timeout 5 --settle 0",
        env=_stub_env(d, UG_ALIVE_CMD="echo running"),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not pending.exists()
    data = json.loads(state.read_text())
    # before the fix: STATE_FILE was empty entrypoint-style -> write_state was
    # a no-op and the job stayed stuck at boot-check-passed forever
    assert data["state"] == "success"


def test_post_start_recovers_state_file_by_sibling_convention(tmp_path):
    """A pending.json WITHOUT a state_file field (legacy shape) still resolves
    the state file via the sibling convention: pending and job state live in
    the same $WORKING/upgrade dir."""
    d = tmp_path
    (d / "version").write_text("2.2.2\n")
    up = d / "upgrade"
    up.mkdir()
    job = "upg-20261014-000000-cccc"
    state = up / f"upgrade-{job}.json"
    state.write_text(json.dumps({
        "job_id": job,
        "state": "boot-check-passed",
        "target": "2.2.2",
        "started_at": "2026-10-14T00:00:00Z",
    }))
    pending = up / "pending.json"
    pending.write_text(json.dumps({
        "job_id": job,
        "old": "2.2.1",
        "target": "2.2.2",
        "venv": str(d / "venv"),
    }))
    proc = run_bash(
        f"bash {SCRIPT} --phase post-start --venv {d}/venv "
        f"--pending-file {pending} --timeout 5 --settle 0",
        env=_stub_env(d, UG_ALIVE_CMD="echo running"),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not pending.exists()
    data = json.loads(state.read_text())
    assert data["state"] == "success"


# ---------------------------------------------------------------------------
# P1-2 (shiyiyue #1364 review): post-start rollback must use a restart
# primitive that exists in the worker image (no supervisor — PID1 is the
# qwenpaw-worker wrapper) and must report its true outcome: a failed rollback
# exits NON-zero (4) with a FATAL banner, a verified recovery exits 3 —
# never 0, which the entrypoint reads as "keep the new runtime".
# ---------------------------------------------------------------------------

def _seed_two_phase_context(d: Path, job: str):
    """Pre-seed what the entrypoint sees after pre-restart + a passed
    boot-check: pending.json (carrying state_file) + a state file parked at
    boot-check-passed."""
    up = d / "upgrade"
    up.mkdir()
    state = up / f"upgrade-{job}.json"
    state.write_text(json.dumps({
        "job_id": job,
        "state": "boot-check-passed",
        "current": "2.2.1",
        "target": "2.2.2",
        "started_at": "2026-10-14T00:00:00Z",
    }))
    pending = up / "pending.json"
    pending.write_text(json.dumps({
        "job_id": job,
        "old": "2.2.1",
        "target": "2.2.2",
        "venv": str(d / "venv"),
        "state_file": str(state),
    }))
    return pending, state


def test_post_start_rollback_failure_exits_fatal_not_zero(tmp_path):
    """P1-2 (shiyiyue case): the worker image has no supervisor, so the
    supervisorctl restart primitive simply is not there; a failed rollback
    must be FATAL — exit 4, loud banner, job state rollback_failed, pending
    kept — never exit 0 (the entrypoint would keep the broken new runtime)."""
    d = tmp_path
    (d / "version").write_text("2.2.1\n")
    calls = d / "calls"
    pending, state = _seed_two_phase_context(d, "upg-20261014-000000-eeee")
    env = _stub_env(
        d,
        UG_VERSION_CMD="exit 1",  # app dead: the target gate times out
        UG_INSTALL_CMD=f'echo install-{{target}} >> {calls}; true',
        UG_PATCH_CMD=f'echo patch-{{target}} >> {calls}; true',
        # no supervisor in the worker image: the primitive is absent
        UG_RESTART_CMD="supervisorctl restart app",
    )
    proc = run_bash(
        f"bash {SCRIPT} --phase post-start --venv {d}/venv "
        f"--pending-file {pending} --timeout 2 --settle 0",
        env=env,
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "FATAL: ROLLBACK FAILED" in proc.stdout + proc.stderr
    data = json.loads(state.read_text())
    assert data["state"] == "rollback_failed"
    assert pending.exists()  # kept for manual retry


def test_post_start_rollback_recovered_exit_code(tmp_path):
    """P1-2: a post-start rollback that restores AND verifies the old runtime
    (supervisorctl mode re-verifies in-process) is a successful recovery:
    exit 3, job state rolled_back, pending removed."""
    d = tmp_path
    (d / "version").write_text("2.2.1\n")  # stuck on the old version
    calls = d / "calls"
    pending, state = _seed_two_phase_context(d, "upg-20261014-000000-gggg")
    env = _stub_env(
        d,
        UG_VERSION_CMD=f"cat {d}/version",
        # the install stub actually moves the version file
        UG_INSTALL_CMD=f'echo install-{{target}} >> {calls}; printf %s {{target}} > {d}/version',
        UG_PATCH_CMD=f'echo patch-{{target}} >> {calls}; true',
        UG_RESTART_CMD=f'echo restart >> {calls}; true',
    )
    proc = run_bash(
        f"bash {SCRIPT} --phase post-start --venv {d}/venv "
        f"--pending-file {pending} --timeout 3 --settle 0",
        env=env,
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert ("SUMMARY: status=recovered phase=post-start old=2.2.1 "
            "target=2.2.2 reason=post-start-health-failed") in proc.stdout
    got = calls.read_text().splitlines()
    assert "install-2.2.1" in got  # rollback reinstalled the old version
    assert "restart" in got
    assert not pending.exists()
    data = json.loads(state.read_text())
    assert data["state"] == "rolled_back"


def test_post_start_rollback_kill_mode_no_supervisor(tmp_path):
    """P1-2: worker images restart by killing the app child (the UG hook
    stubs the primitive here — never a real /proc kill). Kill mode cannot
    re-verify in-process (the wrapper exits with the app child), so recovery
    verification is delegated to the next boot's wrapper version gate:
    exit 3, job rolled_back, pending removed, and no supervisorctl involved."""
    d = tmp_path
    (d / "version").write_text("2.2.1\n")
    calls = d / "calls"
    pending, state = _seed_two_phase_context(d, "upg-20261014-000000-hhhh")
    env = _stub_env(
        d,
        UG_VERSION_CMD=f"cat {d}/version",
        UG_INSTALL_CMD=f'echo install-{{target}} >> {calls}; printf %s {{target}} > {d}/version',
        UG_PATCH_CMD=f'echo patch-{{target}} >> {calls}; true',
        UG_RESTART_CMD=f'echo restart >> {calls}; true',
    )
    proc = run_bash(
        f"bash {SCRIPT} --phase post-start --venv {d}/venv "
        f"--restart-mode kill --pending-file {pending} --timeout 3 --settle 0",
        env=env,
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "supervisorctl" not in proc.stdout + proc.stderr
    got = calls.read_text().splitlines()
    assert "install-2.2.1" in got
    assert "restart" in got
    assert not pending.exists()
    data = json.loads(state.read_text())
    assert data["state"] == "rolled_back"
