"""Loop budget ledger tests for TeamHarness server.py (INT-9 dual ledger,
Python side).

Covers project["loop"]["budget"] = {tier, iteration_budget, iterations_used,
tokens_used, over_budget}:

- plan_loop initializes the budget from the project tier (default standard)
- record_loop_iteration accumulates iterations_used / over_budget and keeps
  the loop running past budget (flag, don't truncate)
- provenance marks each iteration's budget state (recent 5 retained)
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
        "teamharness_mcp_server_budget_test",
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


class FakeFilesync:
    """Fake for server._filesync: pulls that match a pre-seeded remote file
    write the remote content locally; everything else is recorded and
    returns ok=True."""

    def __init__(self, remote_files: dict[str, dict] | None = None) -> None:
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


def _loop_of(server: object, workspace: Path, project_id: str) -> dict:
    project = server._read_json(server._project_state_path(_arguments(workspace, "resolve_project"), project_id))
    return project.get("loop", {})


def test_plan_loop_budget_init_defaults(tmp_path: Path, monkeypatch) -> None:
    """No tier in the project meta → the budget defaults to the standard
    tier (iteration budget 5) with zero usage."""
    server = _load_server()
    monkeypatch.setattr(server, "_filesync", FakeFilesync())

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {"project_id": "p1", "title": "P1", "status": "active", "tasks": []})

    result = server._projectflow(_arguments(
        workspace, "plan_loop", projectId="p1", maxIterations=10,
        goal="g", stopCondition="done", iterationTemplate="t",
    ))
    assert result.get("ok") is True
    loop = result["loop"]
    assert loop["budget"] == {
        "tier": "standard",
        "iteration_budget": 5,
        "iterations_used": 0,
        "tokens_used": 0,
        "over_budget": False,
    }
    assert loop["provenance"] == []


def test_record_loop_iteration_accumulates_budget(tmp_path: Path, monkeypatch) -> None:
    """Each record_loop_iteration accumulates exactly one iteration; token
    accounting stays 0 (loop history carries no token field) until it lands."""
    server = _load_server()
    monkeypatch.setattr(server, "_filesync", FakeFilesync())

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {"project_id": "p1", "title": "P1", "status": "active", "tasks": []})
    server._projectflow(_arguments(
        workspace, "plan_loop", projectId="p1", maxIterations=10,
        goal="g", stopCondition="done", iterationTemplate="t",
    ))

    for iteration in (1, 2):
        result = server._projectflow(_arguments(
            workspace, "record_loop_iteration", projectId="p1",
            iteration=iteration, decision="continue", summary=f"s{iteration}",
        ))
        assert result.get("ok") is True

    loop = _loop_of(server, workspace, "p1")
    budget = loop["budget"]
    assert budget["tier"] == "standard"
    assert budget["iteration_budget"] == 5
    assert budget["iterations_used"] == 2
    assert budget["tokens_used"] == 0
    assert budget["over_budget"] is False
    assert len(loop["history"]) == 2
    assert len(loop["provenance"]) == 2
    assert loop["provenance"][-1] == {
        "iteration": 2, "iterations_used": 2, "iteration_budget": 5, "over_budget": False,
    }


def test_loop_over_budget_flagged_not_truncated(tmp_path: Path, monkeypatch) -> None:
    """A pre-seeded tier (simple → budget 3) is honored: the 4th iteration
    exceeds it. The loop keeps running (flag, don't truncate) and the
    provenance marks the over-budget state."""
    server = _load_server()
    monkeypatch.setattr(server, "_filesync", FakeFilesync())

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {
        "project_id": "p1", "title": "P1", "status": "active", "tasks": [], "tier": "simple",
    })
    server._projectflow(_arguments(
        workspace, "plan_loop", projectId="p1", maxIterations=10,
        goal="g", stopCondition="done", iterationTemplate="t",
    ))

    for iteration in (1, 2, 3):
        server._projectflow(_arguments(
            workspace, "record_loop_iteration", projectId="p1",
            iteration=iteration, decision="continue", summary=f"s{iteration}",
        ))
        loop = _loop_of(server, workspace, "p1")
        assert loop["budget"]["iterations_used"] == iteration
        assert loop["budget"]["over_budget"] is False

    result = server._projectflow(_arguments(
        workspace, "record_loop_iteration", projectId="p1",
        iteration=4, decision="continue", summary="s4",
    ))
    assert result.get("ok") is True
    loop = result["loop"]
    assert loop["status"] == "running"
    assert len(loop["history"]) == 4
    budget = loop["budget"]
    assert budget["tier"] == "simple"
    assert budget["iteration_budget"] == 3
    assert budget["iterations_used"] == 4
    assert budget["over_budget"] is True
    assert loop["provenance"][-1] == {
        "iteration": 4, "iterations_used": 4, "iteration_budget": 3, "over_budget": True,
    }
    assert len(loop["provenance"]) <= 5
