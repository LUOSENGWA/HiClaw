"""INT-5: structured illegal-transition validation + SIG-MISMATCH emission.

Covers the transition-validation hardening on top of the existing
_assert_transition/_transition_task layer (no rebuild):
- legal transitions keep working (status + history written, no event)
- illegal transition -> IllegalTransitionError carrying from/to/action,
  state unchanged, structured error dict at the taskflow dispatch layer
- terminal states (the 4 values of TERMINAL_TASK_STATUSES) rejected with a
  "terminal state" message; unknown states get a distinct "unexpected state"
  message (no "terminal")
- SIG-MISMATCH emitted on rejection (deterministic id, idempotent re-reject)
- static audit: every task/task_meta status write sits in the whitelisted
  functions (_transition_task entry + _accept_task_result declared guard
  paths) — the detector self-proves it catches an injected bypass
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_DIR = REPO_ROOT / "plugins" / "teamharness" / "mcp"
SERVER_PATH = MCP_DIR / "server.py"

EVENT_FIELDS = {
    "event_id", "task_id", "loop_id", "event_type", "is_signal",
    "source_event", "ref", "fingerprint", "iteration_index", "payload",
    "occurred_at",
}

# Status writes are only legal inside the single transition entry
# (_transition_task) and the accept reconciliation paths (#1183 guard).
STATUS_WRITE_RE = re.compile(r'(?<!\w)(?:task|task_meta)\["status"\]\s*=')
WHITELIST_FUNCS = {"_transition_task", "_accept_task_result"}


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(
        "teamharness_mcp_server_transition_test", SERVER_PATH
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


def _write_task(workspace: Path, task_id: str, meta: dict[str, Any]) -> None:
    _write_json(workspace / "shared" / "tasks" / task_id / "meta.json", meta)


def _team(server: Any) -> str:
    return str(server._runtime_team_name() or "").strip() or "global"


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


def test_legal_transition_passes(server: Any, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_filesync", FakeFilesync())
    workspace = tmp_path / "agent"
    task_id = "int5-task-legal"
    _write_task(workspace, task_id, {"task_id": task_id, "status": "assigned", "project_id": ""})
    args = _arguments(workspace, "ack_task", taskId=task_id)
    task = server._load_task(args, task_id)

    old = server._transition_task(args, task, task_id, "in_progress", "ack_task")

    assert old == "assigned"
    assert task["status"] == "in_progress"
    on_disk = server._load_task(args, task_id)
    assert on_disk["status"] == "in_progress"
    entry = on_disk["history"][0]
    assert entry["from"] == "assigned"
    assert entry["to"] == "in_progress"
    assert entry["action"] == "ack_task"
    # A legal transition must not emit any signal event.
    events_dir = workspace / "shared" / "events"
    assert not events_dir.exists() or not any(events_dir.rglob("*.json"))


def test_illegal_transition_rejected(server: Any, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_filesync", FakeFilesync())
    monkeypatch.setenv("AGENTTEAMS_AGENT_ROLE", "leader")
    workspace = tmp_path / "agent"
    task_id = "int5-task-illegal"
    # "bogus" is neither in the transition table nor terminal: cancel reaches
    # the _assert_transition unknown-state branch through the dispatch layer
    # (the per-action pre-checks do not intercept it).
    _write_task(workspace, task_id, {"task_id": task_id, "status": "bogus", "project_id": ""})
    args = _arguments(workspace, "cancel_task", taskId=task_id, reason="audit probe")
    task = server._load_task(args, task_id)
    before = server._read_json(server._task_state_path(args, task_id))

    # Validation layer: structured exception with the from/to/action triple.
    with pytest.raises(server.IllegalTransitionError) as excinfo:
        server._transition_task(args, task, task_id, "cancelled", "cancel_task")
    exc = excinfo.value
    assert isinstance(exc, ValueError)  # internal protocol unchanged
    assert exc.from_status == "bogus"
    assert exc.to_status == "cancelled"
    assert exc.action == "cancel_task"
    assert str(exc).startswith("illegal_transition:")
    # State unchanged (in memory and on disk).
    assert task["status"] == "bogus"
    assert server._read_json(server._task_state_path(args, task_id)) == before

    # Dispatch layer: the same rejection returns the structured error dict.
    result = server._taskflow(args)
    assert result["ok"] is False
    assert result["tool"] == "taskflow"
    assert result["action"] == "cancel_task"
    assert result["error"] == "illegal_transition"
    assert result["from"] == "bogus"
    assert result["to"] == "cancelled"
    assert result["detail"].startswith("illegal_transition: cancel_task:")
    assert "unexpected state bogus" in result["detail"]
    assert "terminal" not in result["detail"]


def test_terminal_state_immutable(server: Any) -> None:
    # The 4 values of TERMINAL_TASK_STATUSES — PARTIAL/INTERRUPTED are not
    # terminal task statuses (#1183 moved PARTIAL out of the accepted set).
    for terminal in ("completed", "revision", "blocked", "cancelled"):
        with pytest.raises(server.IllegalTransitionError) as excinfo:
            server._assert_transition(terminal, "in_progress", "ack_task")
        message = str(excinfo.value)
        assert "terminal state" in message
        assert "(immutable)" in message
        assert terminal in message

    # Unknown (non-terminal) state: distinct message, no "terminal".
    with pytest.raises(server.IllegalTransitionError) as excinfo:
        server._assert_transition("no_such_state", "in_progress", "ack_task")
    message = str(excinfo.value)
    assert "terminal" not in message
    assert "unexpected state" in message

    # Same-state re-entry stays a legal no-op, terminal states included.
    server._assert_transition("completed", "completed", "ack_task")


def test_mismatch_signal_emitted(server: Any, tmp_path: Path, monkeypatch: Any) -> None:
    fake = FakeFilesync()
    monkeypatch.setattr(server, "_filesync", fake)
    workspace = tmp_path / "agent"
    task_id = "int5-task-mismatch"
    _write_task(workspace, task_id, {"task_id": task_id, "status": "in_progress", "project_id": ""})
    args = _arguments(workspace, "delegate_task", taskId=task_id)
    task = server._load_task(args, task_id)

    # in_progress -> assigned is not in the transition table.
    with pytest.raises(server.IllegalTransitionError):
        server._transition_task(args, task, task_id, "assigned", "delegate_task")

    team = _team(server)
    meta_bytes = (workspace / "shared" / "tasks" / task_id / "meta.json").read_bytes()
    fingerprint = hashlib.sha256(meta_bytes).hexdigest()
    expected_id = server._signal_event_id(
        team, "", task_id, "", "SIG-MISMATCH", "mismatch_recorded", None, fingerprint, 0, 0,
    )
    events_dir = workspace / "shared" / "events" / team
    files = sorted(events_dir.glob("*.json"))
    assert [f.name for f in files] == [f"{expected_id}.json"]  # deterministic id
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(data) == EVENT_FIELDS  # exactly the 11 schema fields
    assert data["event_id"] == expected_id
    assert data["event_type"] == "SIG-MISMATCH"
    assert data["is_signal"] is True
    assert data["task_id"] == task_id
    assert data["loop_id"] is None
    assert data["iteration_index"] is None
    assert data["source_event"] == {"origin": "mismatch_recorded", "request_id": None}
    assert data["ref"] == f"shared/tasks/{task_id}/meta.json"
    assert data["fingerprint"] == hashlib.sha256(
        (workspace / "shared" / "tasks" / task_id / "meta.json").read_bytes()
    ).hexdigest()
    assert data["payload"] == {
        "from_status": "in_progress",
        "to_status": "assigned",
        "action": "delegate_task",
    }

    # Re-rejecting the same logical event is idempotent: no second object,
    # no second mirror push.
    task2 = server._load_task(args, task_id)
    with pytest.raises(server.IllegalTransitionError):
        server._transition_task(args, task2, task_id, "assigned", "delegate_task")
    assert [f.name for f in sorted(events_dir.glob("*.json"))] == [f"{expected_id}.json"]
    pushes = [c for c in fake.calls if c.get("action") == "push"]
    assert [c["path"] for c in pushes] == [f"shared/events/{team}/{expected_id}.json"]


def _function_at_line(lines: list[str], index: int) -> str | None:
    """Top-level function owning the line at (0-based) index."""
    owner: str | None = None
    for i, line in enumerate(lines):
        if i > index:
            break
        match = re.match(r"^def (\w+)", line)
        if match:
            owner = match.group(1)
    return owner


def _status_write_violations(source: str) -> list[tuple[int, str, str | None]]:
    """(line_no, text, owner) for status writes outside the whitelist."""
    lines = source.splitlines()
    violations: list[tuple[int, str, str | None]] = []
    for i, line in enumerate(lines):
        if not STATUS_WRITE_RE.search(line):
            continue
        owner = _function_at_line(lines, i)
        if owner not in WHITELIST_FUNCS:
            violations.append((i + 1, line.strip(), owner))
    return violations


def test_status_write_coverage_audit() -> None:
    source = SERVER_PATH.read_text(encoding="utf-8")
    lines = source.splitlines()
    write_lines = [i + 1 for i, line in enumerate(lines) if STATUS_WRITE_RE.search(line)]
    assert write_lines, "expected status write points in server.py"
    # Every write point sits in a whitelisted function.
    assert _status_write_violations(source) == []
    # QC self-proof: an injected bypass write is detected (would go red).
    probe = (
        source
        + "\n\ndef _audit_probe_bypass(task: dict[str, Any]) -> None:\n"
        + '    task["status"] = "submitted"  # probe\n'
    )
    violations = _status_write_violations(probe)
    assert len(violations) == 1
    assert violations[0][2] == "_audit_probe_bypass"
