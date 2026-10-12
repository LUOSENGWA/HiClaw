"""Orchestrator contract tests for ``upgrade_qwenpaw_runtime.sh``.

The heavy path matrix lives in the script's own ``--selftest`` (11
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
    assert "SELFTEST: 11/11 PASS" in proc.stdout


def test_boot_check_success_removes_pending(tmp_path):
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
    assert "SUMMARY: status=ok phase=boot-check target=2.2.2" in proc.stdout
    assert not pending.exists()
    data = json.loads(state.read_text())
    assert data["state"] == "success"
    assert data["finished_at"]


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
