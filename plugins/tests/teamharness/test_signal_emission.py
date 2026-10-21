"""S2 signal emission (INT-3, #1394 aligned) tests for TeamHarness server.py.

Covers the Python side of the shared contract:
- id_rule: the cross-language fixed vector (Go's signal_events_test.go pins
  the same value)
- list_events: types / taskId / since filters + cursor pagination
- record_loop_iteration: SIG-DECISION + conditional SIG-VERDICT
- submit_task / accept_task_result: SIG-COMPLETE
- idempotency: a re-emit of the same logical event is one object on disk
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_DIR = REPO_ROOT / "plugins" / "teamharness" / "mcp"
CONTRACT_PATH = REPO_ROOT / "plugins" / "teamharness" / "contracts" / "signal-mapping.json"

# Same vector as signal_events_test.go: first 16 hex of sha256 of
# "alpha-team|t1|SIG-DECISION|project_paused|0".
FIXED_VECTOR = "ev_2718c107ecfba1e8"

EVENT_FIELDS = {
    "event_id", "task_id", "loop_id", "event_type", "is_signal",
    "source_event", "ref", "fingerprint", "iteration_index", "payload",
    "occurred_at",
}


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(
        "teamharness_mcp_server_signal_test", MCP_DIR / "server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(MCP_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(MCP_DIR))
    return module


@pytest.fixture()
def server() -> Any:
    return _load_server()


def _arguments(workspace: Path, action: str, **payload: Any) -> dict[str, Any]:
    return {"workspaceDir": str(workspace), "action": action, "payload": payload}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _team(server: Any) -> str:
    return str(server._runtime_team_name() or "").strip() or "global"


def _events_dir(server: Any, workspace: Path) -> Path:
    return workspace / "shared" / "events" / _team(server)


def _read_events(server: Any, workspace: Path) -> list[dict[str, Any]]:
    directory = _events_dir(server, workspace)
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


class FakeFilesync:
    """Records _filesync calls (pull/push) instead of touching the network."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {
            "ok": True,
            "tool": "filesync",
            "action": arguments.get("action"),
            "path": arguments.get("path"),
        }


def _write_project(workspace: Path, project_id: str, meta: dict[str, Any]) -> None:
    _write_json(workspace / "shared" / "projects" / project_id / "meta.json", meta)


def _write_task(workspace: Path, task_id: str, meta: dict[str, Any]) -> None:
    _write_json(workspace / "shared" / "tasks" / task_id / "meta.json", meta)


def test_signal_id_fixed_vector_and_rule(server: Any) -> None:
    assert server._signal_event_id("alpha-team", "t1", "SIG-DECISION", "project_paused", 0) == FIXED_VECTOR
    # attempt changes the id: a retry of a different attempt is a different event.
    assert server._signal_event_id("alpha-team", "t1", "SIG-DECISION", "project_paused", 1) != FIXED_VECTOR
    # submit vs accept of the same task hash distinct source_events — no collision.
    assert server._signal_event_id("t1", "task1", "SIG-COMPLETE", "task_submitted", 0) != server._signal_event_id(
        "t1", "task1", "SIG-COMPLETE", "task_accepted", 0
    )


def test_contract_matches_py_emitters(server: Any) -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert len(contract["events"]) == 14
    levels = set(contract["levels"])
    by_source_event: dict[str, dict[str, Any]] = {}
    for row in contract["events"]:
        for source_event in row["source_events"]:
            by_source_event[source_event] = row

    for emitter, (source_event, event_type, _origin) in server._SIGNAL_EMITTERS.items():
        row = by_source_event.get(source_event)
        assert row is not None, f"contract row for source_event {source_event} missing"
        assert f"py:{emitter}" in row["emitters"], f"{emitter}: py:{emitter} not in contract row emitters {row['emitters']}"
        assert row["event_type"] == event_type
        assert row["level"] in levels


def test_emit_idempotent_local(server: Any, tmp_path: Path, monkeypatch: Any) -> None:
    fake = FakeFilesync()
    monkeypatch.setattr(server, "_filesync", fake)
    workspace = tmp_path / "agent"
    args = _arguments(workspace, "noop")

    first = server._emit_signal_event(
        args, "record_loop_iteration", task_id="", iteration_index=1,
        ref="shared/projects/p1/meta.json", ref_bytes=b'{"project_id": "p1"}',
        payload={"project_id": "p1"},
    )
    assert first is True
    # Retry of the same logical event (same id input) — must be a no-op,
    # even with a changed payload.
    second = server._emit_signal_event(
        args, "record_loop_iteration", task_id="", iteration_index=1,
        ref="shared/projects/p1/meta.json", ref_bytes=b'{"project_id": "p1", "retry": true}',
        payload={"project_id": "p1", "note": "retry"},
    )
    assert second is False

    files = sorted(_events_dir(server, workspace).glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(data) == EVENT_FIELDS  # exactly the 11 schema fields
    assert data["event_id"] == files[0].stem
    assert data["payload"] == {"project_id": "p1"}  # original kept, retry dropped
    # The mirror push fired exactly once (first emit only).
    assert [c for c in fake.calls if c.get("action") == "push"] == [
        {
            "workspaceDir": str(workspace),
            "action": "push",
            "payload": {},
            "path": f"shared/events/{_team(server)}/{files[0].name}",
        }
    ]


def test_list_events_filters_and_pagination(server: Any, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_filesync", FakeFilesync())
    workspace = tmp_path / "agent"
    team = _team(server)
    directory = _events_dir(server, workspace)
    directory.mkdir(parents=True, exist_ok=True)

    def seed(event_id: str, event_type: str, task_id: str, occurred_at: str, payload: dict[str, Any]) -> None:
        (directory / f"{event_id}.json").write_text(json.dumps({
            "event_id": event_id,
            "task_id": task_id,
            "loop_id": None,
            "event_type": event_type,
            "is_signal": True,
            "source_event": {"origin": "submit", "request_id": None},
            "ref": "shared/projects/p1/meta.json",
            "fingerprint": "0" * 64,
            "iteration_index": None,
            "payload": payload,
            "occurred_at": occurred_at,
        }), encoding="utf-8")

    seed("ev_01", "SIG-DECISION", "t1", "2026-10-01T00:00:01Z", {"n": 1})
    seed("ev_02", "SIG-DECISION", "t2", "2026-10-01T00:00:02Z", {"n": 2})
    seed("ev_03", "SIG-DECISION", "t1", "2026-10-01T00:00:03Z", {"n": 3})
    seed("ev_04", "SIG-COMPLETE", "t1", "2026-10-01T00:00:04Z", {"n": 4})
    seed("ev_05", "SIG-VERDICT", "", "2026-10-01T00:00:05Z", {"n": 5})

    def query(**payload: Any) -> dict[str, Any]:
        return server._projectflow(_arguments(workspace, "list_events", team=team, **payload))

    # No filters: all 5, in (occurred_at, event_id) order.
    res = query()
    assert res["ok"] is True
    assert [e["event_id"] for e in res["events"]] == ["ev_01", "ev_02", "ev_03", "ev_04", "ev_05"]
    assert res["count"] == 5 and res["next_cursor"] is None

    # types filter.
    assert [e["event_id"] for e in query(types=["SIG-DECISION"])["events"]] == ["ev_01", "ev_02", "ev_03"]
    assert [e["event_id"] for e in query(types=["SIG-COMPLETE", "SIG-VERDICT"])["events"]] == ["ev_04", "ev_05"]

    # taskId filter (task_id match).
    assert [e["event_id"] for e in query(taskId="t1")["events"]] == ["ev_01", "ev_03", "ev_04"]

    # since filter (inclusive).
    assert [e["event_id"] for e in query(since="2026-10-01T00:00:03Z")["events"]] == ["ev_03", "ev_04", "ev_05"]

    # Pagination: limit=2, then follow the cursor.
    page1 = query(limit=2)
    assert [e["event_id"] for e in page1["events"]] == ["ev_01", "ev_02"]
    assert page1["next_cursor"] is not None
    page2 = query(limit=2, cursor=page1["next_cursor"])
    assert [e["event_id"] for e in page2["events"]] == ["ev_03", "ev_04"]
    page3 = query(limit=2, cursor=page2["next_cursor"])
    assert [e["event_id"] for e in page3["events"]] == ["ev_05"]
    assert page3["next_cursor"] is None

    # A corrupt cursor is rejected, not guessed at.
    bad = query(cursor="not-a-valid-cursor")
    assert bad["ok"] is False
    assert "cursor" in bad["error"]


def test_record_loop_iteration_emits(server: Any, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_filesync", FakeFilesync())
    monkeypatch.setattr(server, "_sync_project", lambda *a, **k: True)
    loop = {"goal": "g", "stop_condition": "s", "max_iterations": 3, "current_iteration": 0,
            "status": "running", "tasks": [], "history": []}

    workspace = tmp_path / "agent"
    _write_project(workspace, "p1", {"project_id": "p1", "title": "P1", "status": "active", "plan_type": "loop", "loop": loop})
    res = server._projectflow(_arguments(workspace, "record_loop_iteration", projectId="p1", iteration=1,
                                         decision="continue", summary="s1"))
    assert res["ok"] is True
    evs = _read_events(server, workspace)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["event_type"] == "SIG-DECISION"
    assert ev["task_id"] == ""  # project-level event
    assert ev["loop_id"] is None  # main baseline has no loop_id field
    assert ev["iteration_index"] == 1
    assert ev["source_event"] == {"origin": "decision_lock", "request_id": None}
    assert ev["payload"]["decision"] == "continue"
    assert ev["payload"]["iteration"] == 1

    # Conditional SIG-VERDICT: a verdict dict (or evidenceRef) rides a second event.
    workspace2 = tmp_path / "agent2"
    _write_project(workspace2, "p2", {"project_id": "p2", "title": "P2", "status": "active", "plan_type": "loop", "loop": loop})
    res2 = server._projectflow(_arguments(workspace2, "record_loop_iteration", projectId="p2", iteration=1,
                                          decision="stop_success", summary="done",
                                          verdict={"score": 9}, evidenceRef="shared/projects/p2/eval.md"))
    assert res2["ok"] is True
    evs2 = _read_events(server, workspace2)
    # Order-agnostic: the two event objects are separate files.
    assert sorted(e["event_type"] for e in evs2) == ["SIG-DECISION", "SIG-VERDICT"]
    verdict = next(e for e in evs2 if e["event_type"] == "SIG-VERDICT")
    assert verdict["payload"]["verdict"] == {"score": 9}
    assert verdict["payload"]["evidence_ref"] == "shared/projects/p2/eval.md"
    assert verdict["source_event"]["origin"] == "critic_verdict"


def _write_project_and_task(workspace: Path, server: Any, *, task_status: str = "in_progress") -> None:
    project = {
        "project_id": "p1",
        "title": "P1",
        "status": "active",
        "tasks": [{"task_id": "t1", "title": "T1", "assigned_to": "@worker:example.test",
                   "depends_on": [], "status": task_status}],
        "requester_report": {"pending": False, "sent_at": "2026-08-13T08:00:00Z"},
    }
    task = {"task_id": "t1", "project_id": "p1", "room_id": "!task:example.test", "status": task_status}
    server._write_json(workspace / "shared" / "projects" / "p1" / "meta.json", project)
    server._write_json(workspace / "shared" / "tasks" / "t1" / "meta.json", task)


def test_accept_submit_emits_complete(server: Any, tmp_path: Path, monkeypatch: Any) -> None:
    workspace = tmp_path / "agent"
    fake = FakeFilesync()
    monkeypatch.setattr(server, "_filesync", fake)
    monkeypatch.setattr(server, "_pull_project", lambda *a, **k: True)
    monkeypatch.setattr(server, "_publish_task_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(server, "_publish_project_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(server, "_sync_task", lambda *a, **k: True)
    monkeypatch.setattr(server, "_sync_project", lambda *a, **k: True)
    _write_project_and_task(workspace, server)

    submit = server._taskflow({
        "role": "worker",
        "workspaceDir": str(workspace),
        "action": "submit_task",
        "payload": {
            "taskId": "t1",
            "status": "SUCCESS",
            "summary": "done",
            "deliverables": ["shared/tasks/t1/result.md"],
        },
    })
    assert submit["ok"] is True
    team = _team(server)
    assert [e["event_type"] for e in _read_events(server, workspace)] == ["SIG-COMPLETE"]
    assert submit["task"]["submission_id"]

    accept = server._projectflow({
        "role": "leader",
        "workspaceDir": str(workspace),
        "action": "accept_task_result",
        "payload": {
            "projectId": "p1",
            "taskId": "t1",
            "submissionId": submit["task"]["submission_id"],
            "resultStatus": "SUCCESS",
            "summary": "done",
        },
    })
    assert accept["ok"] is True
    evs = _read_events(server, workspace)
    # Two SIG-COMPLETE events (submit + accept) with distinct deterministic ids.
    assert [e["event_type"] for e in evs] == ["SIG-COMPLETE", "SIG-COMPLETE"]
    assert len({e["event_id"] for e in evs}) == 2
    assert all(e["task_id"] == "t1" for e in evs)
    # The mirror pushes targeted the event objects, not the task/project.
    pushes = [c for c in fake.calls if c.get("action") == "push"]
    assert len(pushes) == 2
    assert all(p["path"].startswith(f"shared/events/{team}/") for p in pushes)
