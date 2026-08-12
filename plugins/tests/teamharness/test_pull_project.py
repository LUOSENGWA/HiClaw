"""W-PR-2 read-before-read tests for TeamHarness server.py.

Covers the _pull_project implementation and the _projectflow/_taskflow
entry-point unified pull:

- single-file pull of shared/projects/{id}/meta.json (not a directory
  mirror) — asserted by mocking _filesync and checking the path passed
- plan.md re-render when the authoritative fields change (D2)
- field preservation back-fill (reply_route / source_room_id / team_id)
- scalar audit fields survive read-modify-write (E1)
- entry-point unified pull: projectflow read actions and taskflow actions
  pull before dispatch; create actions are excluded; taskId-indirect
  resolution works
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_DIR = REPO_ROOT / "plugins" / "teamharness" / "mcp"


def _load_server() -> object:
    spec = importlib.util.spec_from_file_location(
        "teamharness_mcp_server_pull_test",
        MCP_DIR / "server.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(MCP_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(MCP_DIR))
    return module


def _arguments(workspace: Path, action: str, **payload) -> dict:
    return {
        "workspaceDir": str(workspace),
        "action": action,
        "payload": payload,
    }


def _write_project(workspace: Path, project_id: str, meta: dict) -> None:
    path = workspace / "shared" / "projects" / project_id / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _write_task(workspace: Path, task_id: str, meta: dict) -> None:
    path = workspace / "shared" / "tasks" / task_id / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


class FakeFilesync:
    """Fake for server._filesync that simulates the real mc cp behavior for
    pulls: when a pull path matches a pre-seeded remote file, the remote
    content is written to the local path (exactly what mc cp would do). Other
    actions are recorded and return ok=True."""

    def __init__(self, remote_files: dict[str, dict] | None = None) -> None:
        # remote_files: normalized shared path (e.g. "shared/projects/p1/meta.json")
        # -> meta dict to write locally on pull.
        self.remote_files = remote_files or {}
        self.calls: list[dict] = []

    def __call__(self, arguments: dict):
        self.calls.append(dict(arguments))
        action = arguments.get("action")
        path = str(arguments.get("path") or "").rstrip("/")
        if action == "pull" and path in self.remote_files:
            workspace = Path(arguments.get("workspaceDir") or "")
            local = workspace / path
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(json.dumps(self.remote_files[path], ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "tool": "filesync", "action": action, "path": path}


def test_pull_project_single_file(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    fake = FakeFilesync()
    monkeypatch.setattr(server, "_filesync", fake)

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {"project_id": "p1", "title": "Old", "status": "active", "tasks": []})

    pulled = server._pull_project(_arguments(workspace, "resolve_project"), "p1")
    assert pulled is True

    # Must be a single-file pull of meta.json, not a directory mirror.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["action"] == "pull"
    assert call["path"] == "shared/projects/p1/meta.json"


def test_pull_project_rerenders_plan_on_status_change(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    fake = FakeFilesync(remote_files={
        "shared/projects/p1/meta.json": {"project_id": "p1", "title": "P1", "status": "paused", "tasks": []},
    })
    monkeypatch.setattr(server, "_filesync", fake)

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {"project_id": "p1", "title": "P1", "status": "active", "tasks": []})
    plan_path = workspace / "shared" / "projects" / "p1" / "plan.md"
    assert not plan_path.exists()

    server._pull_project(_arguments(workspace, "resolve_project"), "p1")

    assert plan_path.exists()
    text = plan_path.read_text(encoding="utf-8")
    assert "paused" in text


def test_pull_project_no_rerender_when_unchanged(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    fake = FakeFilesync(remote_files={
        "shared/projects/p1/meta.json": {"project_id": "p1", "title": "P1", "status": "active", "tasks": [], "plan_type": "dag"},
    })
    monkeypatch.setattr(server, "_filesync", fake)

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {"project_id": "p1", "title": "P1", "status": "active", "tasks": [], "plan_type": "dag"})
    plan_path = workspace / "shared" / "projects" / "p1" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("original plan", encoding="utf-8")

    # Same content pulled -> no re-render.
    server._pull_project(_arguments(workspace, "resolve_project"), "p1")

    assert plan_path.read_text(encoding="utf-8") == "original plan"


def test_pull_project_preserves_fields(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    fake = FakeFilesync(remote_files={
        # Remote older copy omits the fields the local copy has.
        "shared/projects/p1/meta.json": {"project_id": "p1", "title": "P1", "status": "active", "tasks": []},
    })
    monkeypatch.setattr(server, "_filesync", fake)

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {
        "project_id": "p1", "title": "P1", "status": "active", "tasks": [],
        "reply_route": {"target_session": "!local:server"},
        "source_room_id": "!room:server",
        "team_id": "alpha-team",
    })

    server._pull_project(_arguments(workspace, "resolve_project"), "p1")

    meta = json.loads((workspace / "shared/projects/p1/meta.json").read_text(encoding="utf-8"))
    assert meta["reply_route"] == {"target_session": "!local:server"}
    assert meta["source_room_id"] == "!room:server"
    assert meta["team_id"] == "alpha-team"


def test_pull_project_scalar_fields_survive(tmp_path: Path, monkeypatch) -> None:
    """E1: scalar audit fields (updated_by/updated_at/pause_reason) written by
    the Controller survive the worker's read-modify-write because the worker
    pulls them and then re-writes the whole dict."""
    server = _load_server()
    fake = FakeFilesync(remote_files={
        "shared/projects/p1/meta.json": {
            "project_id": "p1", "title": "P1", "status": "paused", "tasks": [],
            "updated_by": "admin", "updated_at": "2026-08-12T00:00:00Z", "pause_reason": "review",
        },
    })
    monkeypatch.setattr(server, "_filesync", fake)

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {"project_id": "p1", "title": "P1", "status": "active", "tasks": []})

    server._pull_project(_arguments(workspace, "resolve_project"), "p1")

    meta = json.loads((workspace / "shared/projects/p1/meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "paused"
    assert meta["updated_by"] == "admin"
    assert meta["pause_reason"] == "review"


def test_projectflow_read_action_pulls(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    fake = FakeFilesync()
    monkeypatch.setattr(server, "_filesync", fake)

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {"project_id": "p1", "title": "P1", "status": "active", "tasks": []})

    result = server._projectflow(_arguments(workspace, "resolve_project", projectId="p1"))
    assert result.get("ok") is True
    pulls = [c for c in fake.calls if c["action"] == "pull"]
    assert len(pulls) == 1
    assert pulls[0]["path"] == "shared/projects/p1/meta.json"


def test_projectflow_create_excluded(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    fake = FakeFilesync()
    monkeypatch.setattr(server, "_filesync", fake)

    workspace = tmp_path / "agent"
    result = server._projectflow(_arguments(workspace, "create_project", title="New"))
    assert result.get("ok") is True
    assert [c for c in fake.calls if c["action"] == "pull"] == []


def test_projectflow_taskid_indirect(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    fake = FakeFilesync()
    monkeypatch.setattr(server, "_filesync", fake)

    workspace = tmp_path / "agent"
    _write_task(workspace, "t1", {"task_id": "t1", "project_id": "p1", "status": "planned"})
    _write_project(workspace, "p1", {"project_id": "p1", "title": "P1", "status": "active", "tasks": []})

    # resolve_project may carry only taskId; the entry pull resolves the
    # project via the local task meta.
    result = server._projectflow(_arguments(workspace, "resolve_project", taskId="t1"))
    assert result.get("ok") is True
    pulls = [c for c in fake.calls if c["action"] == "pull"]
    assert len(pulls) == 1
    assert pulls[0]["path"] == "shared/projects/p1/meta.json"


def test_taskflow_actions_pull(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    fake = FakeFilesync()
    monkeypatch.setattr(server, "_filesync", fake)

    workspace = tmp_path / "agent"
    _write_task(workspace, "t1", {"task_id": "t1", "project_id": "p1", "status": "planned"})
    _write_project(workspace, "p1", {"project_id": "p1", "title": "P1", "status": "active", "tasks": []})

    # check_task is a read-only leader action; the entry pull must fire.
    args = _arguments(workspace, "check_task", taskId="t1")
    args["role"] = "leader"
    result = server._taskflow(args)
    assert result.get("ok") is True
    project_pulls = [
        c for c in fake.calls
        if c["action"] == "pull" and c["path"] == "shared/projects/p1/meta.json"
    ]
    assert len(project_pulls) == 1
