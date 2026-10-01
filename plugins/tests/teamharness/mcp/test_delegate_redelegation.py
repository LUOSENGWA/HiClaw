"""B4: delegate_task re-delegation must not be swallowed by txn dedup.

A re-delegation (same task, same room, *different* assignee) is a new
delegation: it must re-prepare the task and send a fresh assignment
notification. A retry (same assignee, event already recorded) must reuse
the recorded event and send nothing.

Incident context: the delegate notification used a stable Matrix
transaction id per task, so the homeserver deduplicated the re-delegation
notification and the new worker never heard about the assignment.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import sys
from pathlib import Path
from typing import Any
import threading

import pytest


MCP_DIR = Path(__file__).resolve().parents[3] / "teamharness" / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import server  # noqa: E402


WORKER_A = "@alice:example.test"
WORKER_B = "@bob:example.test"
ROOM = "!taskroom:example.test"


def _write_assigned_task(
    workspace: Path,
    *,
    task_id: str = "redep-project-01",
    project_id: str = "redep-project",
    assignee: str = WORKER_A,
    event_id: str = "$original-event",
) -> tuple[str, str]:
    project = {
        "project_id": project_id,
        "title": "Re-delegation contract",
        "status": "active",
        "tasks": [
            {
                "task_id": task_id,
                "title": "Produce a result",
                "assigned_to": assignee,
                "depends_on": [],
                "status": "assigned",
            }
        ],
        "requester_report": {"pending": False, "sent_at": "2026-09-29T08:00:00Z"},
    }
    task = {
        "task_id": task_id,
        "project_id": project_id,
        "room_id": ROOM,
        "status": "assigned",
        "assigned_to": assignee,
        "eventId": event_id,
    }
    server._write_json(
        workspace / "shared" / "projects" / project_id / "meta.json", project
    )
    server._write_json(workspace / "shared" / "tasks" / task_id / "meta.json", task)
    return project_id, task_id


def _delegate(
    workspace: Path,
    task_id: str,
    assigned_to: str,
    project_id: str = "redep-project",
) -> dict[str, Any]:
    arguments = {
        "role": "leader",
        "action": "delegate_task",
        "workspaceDir": str(workspace),
        "payload": {
            "projectId": project_id,
            "taskId": task_id,
            "roomId": ROOM,
            "assignedTo": assigned_to,
            "title": "Produce a result",
            "spec": "Do the thing.",
        },
    }
    response = server.call_tool("taskflow", arguments)
    return json.loads(response["content"][0]["text"])


@pytest.fixture
def delegate_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"notify": []}

    def notify(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["notify"].append(kwargs)
        return {
            "sent": True,
            "eventId": f"$fresh-event-{len(calls['notify'])}",
            "roomId": kwargs.get("room_id", ""),
            "assignee": kwargs.get("assignee", ""),
        }

    monkeypatch.setenv("AGENTTEAMS_MATRIX_URL", "http://matrix.example.test")
    monkeypatch.setenv("AGENTTEAMS_WORKER_MATRIX_TOKEN", "test-token")
    monkeypatch.setattr(server, "_pull_project", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server, "_sync_task", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server, "_send_delegate_notification", notify)
    monkeypatch.setattr(
        server,
        "_validate_assignee_membership",
        lambda *_args, **_kwargs: {"ok": True},
    )
    return calls


def test_redelegation_to_different_assignee_sends_fresh_notification(
    tmp_path: Path,
    delegate_side_effects: dict[str, list[Any]],
) -> None:
    _write_assigned_task(tmp_path)

    result = _delegate(tmp_path, "redep-project-01", WORKER_B)

    assert result["ok"] is True
    assert result["notification"].get("reused") is not True
    assert result["task"]["assigned_to"] == WORKER_B
    assert result["task"]["status"] == "assigned"
    assert result["task"]["eventId"] != "$original-event"
    # The new worker actually got a notification.
    assert len(delegate_side_effects["notify"]) == 1
    assert delegate_side_effects["notify"][0]["assignee"] == WORKER_B
    # Audit trail carries the re-delegation note.
    notes = [
        str(entry.get("note") or "")
        for entry in result["task"].get("history", [])
        if isinstance(entry, dict)
    ]
    assert any("re-delegate" in note for note in notes)
    # Project meta follows the new assignee.
    project = server._read_json(
        tmp_path / "shared" / "projects" / "redep-project" / "meta.json"
    )
    task_entry = next(
        t for t in project["tasks"] if t.get("task_id") == "redep-project-01"
    )
    assert task_entry["assigned_to"] == WORKER_B


def test_retry_same_assignee_reuses_recorded_event(
    tmp_path: Path,
    delegate_side_effects: dict[str, list[Any]],
) -> None:
    _write_assigned_task(tmp_path)

    result = _delegate(tmp_path, "redep-project-01", WORKER_A)

    assert result["ok"] is True
    assert result["notification"].get("reused") is True
    assert result["notification"]["eventId"] == "$original-event"
    assert result["task"]["assigned_to"] == WORKER_A
    # No duplicate notification goes out.
    assert delegate_side_effects["notify"] == []


def test_retry_with_different_casing_of_same_assignee_reuses(
    tmp_path: Path,
    delegate_side_effects: dict[str, list[Any]],
) -> None:
    _write_assigned_task(tmp_path)

    result = _delegate(tmp_path, "redep-project-01", "@ALICE:example.test")

    assert result["ok"] is True
    assert result["notification"].get("reused") is True
    assert delegate_side_effects["notify"] == []


def test_delegate_txn_is_unique_per_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each send must use a fresh transaction id (no homeserver dedup)."""
    captured_paths: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:  # noqa: N802
            captured_paths.append(self.path)
            body = json.dumps({"event_id": "$evt"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # silence
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("AGENTTEAMS_MATRIX_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("AGENTTEAMS_WORKER_MATRIX_TOKEN", "test-token")
    try:
        for _ in range(2):
            result = server._send_delegate_notification(
                {},
                room_id=ROOM,
                task_id="txn-task-01",
                title="T",
                assignee=WORKER_A,
                spec="spec",
            )
            assert result.get("sent") is True
        assert len(captured_paths) == 2
        txn_a = captured_paths[0].rsplit("/", 1)[-1]
        txn_b = captured_paths[1].rsplit("/", 1)[-1]
        assert txn_a != txn_b, "transaction id must be unique per send"
        assert re.fullmatch(r"delegate-txn-task-01-[0-9a-f]{12}", txn_a)
    finally:
        httpd.shutdown()
