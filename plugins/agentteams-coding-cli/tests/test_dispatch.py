# -*- coding: utf-8 -*-
"""Dispatch surface tests (design §4.2, gateless per the 10/22 ruling).

Drives a real ``DispatchService`` (real subprocess spawn, real file
state) against fake CLI binaries on a restricted tmp PATH, plus the
HTTP routes through ``TestClient``. The service is not patched; the
wake sender is injected through the constructor — the designed seam.

Env overrides (try/finally, same pattern as the P1 router tests):
- ``QWENPAW_CODING_CLI_STATE_DIR`` -> tmp state dir;
- ``QWENPAW_DISPATCH_WATCH_INTERVAL`` -> 0.1s poll for fast terminal
  states (the production default is 20s, like dispatch_watch.py);
- ``PATH`` -> tmp bin dir plus ``/usr/bin:/bin`` only, so the fake
  CLIs resolve and the real managed CLIs (``qwen`` & co., installed
  under /usr/local) are deterministically absent.
"""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentteams_coding_cli.dispatch import (
    WATCH_INTERVAL_ENV,
    CliNotFoundError,
    DispatchBusyError,
    DispatchService,
)
from agentteams_coding_cli.router import build_router
from agentteams_coding_cli.state import STATE_DIR_ENV

API = "/api/agentteams-coding-cli"

WAKE = {
    "session_id": "matrix:!wake42:matrix.example",
    "agent_id": "AgentTeamGuide",
    "base_url": None,
}


def _make_bin(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)


class Env:
    """Hermetic env: tmp state dir, 0.1s watchdog poll, fake CLI PATH."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.state_dir = tmp_path / "state"
        self.bin_dir = tmp_path / "bin"
        self._saved: dict[str, str] = {}

    def __enter__(self) -> "Env":
        self._saved = dict(os.environ)
        os.environ[STATE_DIR_ENV] = str(self.state_dir)
        os.environ[WATCH_INTERVAL_ENV] = "0.1"
        os.environ["PATH"] = f"{self.bin_dir}:/usr/bin:/bin"
        return self

    def __exit__(self, *exc_info: Any) -> None:
        os.environ.clear()
        os.environ.update(self._saved)


def _wake_recorder() -> tuple[list[dict[str, str]], Any]:
    calls: list[dict[str, str]] = []

    def sender(session_id: str, agent_id: str, base_url: str, text: str) -> None:
        calls.append(
            {
                "session_id": session_id,
                "agent_id": agent_id,
                "base_url": base_url,
                "text": text,
            }
        )

    return calls, sender


async def _wait_terminal(
    service: DispatchService, task_id: str, tries: int = 200
) -> dict:
    for _ in range(tries):
        task = service.get_task(task_id)
        if task is not None and task["state"] != "running":
            return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} still running after {tries} polls")


async def _wait_wake(service: DispatchService, task_id: str, tries: int = 100) -> dict:
    """Poll until wake_status has settled (wake fires after the terminal write)."""
    for _ in range(tries):
        task = service.get_task(task_id)
        status = (task or {}).get("wake_status")
        if status is not None and status != "pending":
            return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} wake_status never settled")


@pytest.mark.asyncio
async def test_spawn_done_wakes(tmp_path) -> None:
    """spawn -> running -> done; log captured; wake got the terminal event.

    A re-dispatch with the same task name is allowed once the first
    run is terminal (claim flag released).
    """
    with Env(tmp_path) as env:
        _make_bin(env.bin_dir, "fakeq", 'echo \'{"type":"assistant","text":"ok"}\'')
        calls, sender = _wake_recorder()
        service = DispatchService(wake_sender=sender)
        task_id = await service.start(
            task_name="t-done",
            prompt="do the thing",
            cwd=None,
            cli="fakeq",
            timeout=30,
            wake=dict(WAKE),
        )
        task = service.get_task(task_id)
        assert task is not None
        assert task["state"] in ("running", "done")
        assert task["pid"]
        assert task["session_id"]
        assert task["wake"] == WAKE
        assert "files_to_touch" not in task  # pure metadata, never persisted

        terminal = await _wait_terminal(service, task_id)
        assert terminal["state"] == "done"
        assert terminal["exit_code"] == 0
        assert terminal["wall_s"] is not None
        assert terminal["log_tail"].strip().startswith("{")
        assert '"text":"ok"' in terminal["log_tail"]
        settled = await _wait_wake(service, task_id)
        assert settled["wake_status"] == "sent"

        assert len(calls) == 1
        assert calls[0]["session_id"] == WAKE["session_id"]
        assert calls[0]["agent_id"] == "AgentTeamGuide"
        assert "终态=done" in calls[0]["text"]
        assert f"dispatch/{task_id}/log" in calls[0]["text"]

        log = service.get_log(task_id)
        assert log is not None
        assert '"text":"ok"' in log

        # claim flag released after terminal: same name may run again
        task_id2 = await service.start(
            task_name="t-done",
            prompt="again",
            cwd=None,
            cli="fakeq",
            timeout=30,
            wake=None,
        )
        terminal2 = await _wait_terminal(service, task_id2)
        assert terminal2["state"] == "done"
        assert terminal2["wake_status"] == "skipped"
        assert len(calls) == 1  # no wake for the second run (wake=None)


@pytest.mark.asyncio
async def test_spawn_failed(tmp_path) -> None:
    """Non-zero exit -> terminal state failed + wake carries exit=1."""
    with Env(tmp_path) as env:
        _make_bin(
            env.bin_dir,
            "fakefail",
            'echo \'{"type":"error","msg":"boom"}\'; exit 1',
        )
        calls, sender = _wake_recorder()
        service = DispatchService(wake_sender=sender)
        task_id = await service.start(
            task_name="t-failed",
            prompt="x",
            cwd=None,
            cli="fakefail",
            timeout=30,
            wake=dict(WAKE),
        )
        terminal = await _wait_terminal(service, task_id)
        assert terminal["state"] == "failed"
        assert terminal["exit_code"] == 1
        settled = await _wait_wake(service, task_id)
        assert settled["wake_status"] == "sent"
        assert "终态=failed" in calls[0]["text"]
        assert "exit=1" in calls[0]["text"]
        log = service.get_log(task_id)
        assert log is not None
        assert '"msg":"boom"' in log


@pytest.mark.asyncio
async def test_timeout_kills(tmp_path) -> None:
    """timeout=1s + a sleeping CLI -> killed, terminal state timeout."""
    with Env(tmp_path) as env:
        _make_bin(env.bin_dir, "fakesleep", "exec sleep 30")
        service = DispatchService(wake_sender=lambda *a: None)
        task_id = await service.start(
            task_name="t-timeout",
            prompt="x",
            cwd=None,
            cli="fakesleep",
            timeout=1,
            wake=None,
        )
        terminal = await _wait_terminal(service, task_id, tries=400)
        assert terminal["state"] == "timeout"
        assert terminal["exit_code"] == -9
        assert terminal["wall_s"] is not None
        assert terminal["wall_s"] < 10
        assert terminal["wake_status"] == "skipped"


@pytest.mark.asyncio
async def test_claim_flag_and_not_found(tmp_path) -> None:
    """Claim flag: same name running -> busy; other names unaffected.

    CLI not on PATH -> CliNotFoundError with an install hint for
    managed CLIs.
    """
    with Env(tmp_path) as env:
        _make_bin(env.bin_dir, "fakesleep", "exec sleep 30")
        service = DispatchService(wake_sender=lambda *a: None)
        task_id = await service.start(
            task_name="held",
            prompt="x",
            cwd=None,
            cli="fakesleep",
            timeout=300,
            wake=None,
        )
        with pytest.raises(DispatchBusyError):
            await service.start(
                task_name="held",
                prompt="y",
                cwd=None,
                cli="fakesleep",
                timeout=300,
                wake=None,
            )
        # a different task name is not blocked (no parallel gate)
        other = await service.start(
            task_name="other",
            prompt="y",
            cwd=None,
            cli="fakesleep",
            timeout=300,
            wake=None,
        )

        with pytest.raises(CliNotFoundError) as excinfo:
            await service.start(
                task_name="n1",
                prompt="x",
                cwd=None,
                cli="no-such-cli-zzz",
                timeout=30,
                wake=None,
            )
        assert "not found on PATH" in str(excinfo.value)
        # restricted PATH has no /usr/local/bin -> 'qwen' absent -> hint
        with pytest.raises(CliNotFoundError) as excinfo2:
            await service.start(
                task_name="n2",
                prompt="x",
                cwd=None,
                cli="qwen",
                timeout=30,
                wake=None,
            )
        assert (
            "POST /api/agentteams-coding-cli/qwen-code/install"
            in str(excinfo2.value)
        )
        # bad cwd -> ValueError
        with pytest.raises(ValueError):
            await service.start(
                task_name="n3",
                prompt="x",
                cwd="/nonexistent-dir-xyz",
                cli="fakesleep",
                timeout=30,
                wake=None,
            )

        # cleanup: kill both children so no orphan sleeps linger
        for tid in (task_id, other):
            t = service.get_task(tid)
            assert t is not None and t["state"] == "running"
            os.kill(t["pid"], 9)
        t1 = await _wait_terminal(service, task_id)
        t2 = await _wait_terminal(service, other)
        assert t1["state"] == "failed"
        assert t1["exit_code"] == -9
        assert t2["state"] == "failed"


@pytest.fixture
def dispatch_http(tmp_path) -> Generator[tuple[TestClient, Env], None, None]:
    with Env(tmp_path) as env:
        _make_bin(env.bin_dir, "fakeq", 'echo \'{"type":"assistant","text":"ok"}\'')
        application = FastAPI()
        application.include_router(
            build_router(), prefix="/api/agentteams-coding-cli"
        )
        with TestClient(application) as client:
            yield client, env


def test_http_dispatch_lifecycle(dispatch_http) -> None:
    """POST 202 -> state file running/done -> log -> list -> 404s."""
    client, _ = dispatch_http
    res = client.post(
        f"{API}/dispatch",
        json={"task": "http-done", "prompt": "do it", "cli": "fakeq", "timeout": 30},
    )
    assert res.status_code == 202
    task_id = res.json()["task_id"]
    assert task_id

    terminal = None
    for _ in range(200):
        got = client.get(f"{API}/dispatch/{task_id}")
        assert got.status_code == 200
        if got.json()["state"] != "running":
            terminal = got.json()
            break
        time.sleep(0.05)
    assert terminal is not None
    assert terminal["state"] == "done"
    assert terminal["exit_code"] == 0
    assert terminal["wake_status"] == "skipped"

    log_res = client.get(f"{API}/dispatch/{task_id}/log")
    assert log_res.status_code == 200
    assert log_res.json()["task_id"] == task_id
    assert '"text":"ok"' in log_res.json()["log"]

    listing = client.get(f"{API}/dispatch").json()["tasks"]
    assert any(
        t["task_id"] == task_id and t["task"] == "http-done" for t in listing
    )

    assert client.get(f"{API}/dispatch/{'0' * 32}").status_code == 404
    assert client.get(f"{API}/dispatch/not-a-hex-id").status_code == 404
    assert client.get(f"{API}/dispatch/{'0' * 32}/log").status_code == 404


def test_http_claim_flag_409(dispatch_http) -> None:
    """A running task file with the same name blocks re-dispatch (409)."""
    client, env = dispatch_http
    task_id = "b" * 32
    dispatch_dir = env.state_dir / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    (dispatch_dir / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "task": "held",
                "cli": "fakeq",
                "state": "running",
                "pid": 999999,
                "started_epoch": 1.0,
            }
        ),
        encoding="utf-8",
    )
    res = client.post(
        f"{API}/dispatch",
        json={"task": "held", "prompt": "x", "cli": "fakeq"},
    )
    assert res.status_code == 409
    assert "already running" in res.json()["detail"]
    assert task_id in res.json()["detail"]


def test_http_cli_not_installed_409(dispatch_http) -> None:
    """CLI not on PATH -> 409 with an install hint; default cli is qwen."""
    client, _ = dispatch_http
    res = client.post(
        f"{API}/dispatch",
        json={"task": "n1", "prompt": "x", "cli": "no-such-cli-zzz"},
    )
    assert res.status_code == 409
    assert "not found on PATH" in res.json()["detail"]

    res = client.post(
        f"{API}/dispatch",
        json={"task": "n2", "prompt": "x", "cli": "qwen"},
    )
    assert res.status_code == 409
    assert (
        "POST /api/agentteams-coding-cli/qwen-code/install"
        in res.json()["detail"]
    )

    # no cli field -> default "qwen" applies (also not on this PATH)
    res = client.post(f"{API}/dispatch", json={"task": "d1", "prompt": "x"})
    assert res.status_code == 409
    assert "'qwen' not found on PATH" in res.json()["detail"]


def test_http_bad_cwd_400(dispatch_http) -> None:
    client, _ = dispatch_http
    res = client.post(
        f"{API}/dispatch",
        json={
            "task": "n3",
            "prompt": "x",
            "cli": "fakeq",
            "cwd": "/nonexistent-dir-xyz",
        },
    )
    assert res.status_code == 400
    assert "cwd is not a directory" in res.json()["detail"]
