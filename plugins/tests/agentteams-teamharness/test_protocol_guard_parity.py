"""W2 protocol guards (issue #1451 W2) — second-tree parity.

``plugins/agentteams-teamharness`` carries a whole-tree compatibility
copy of ``plugins/teamharness`` (see its DEDUPE.md; the drift is
enforced by ``golden/test_compatibility_copy_drift.py``). The
three-blocker fixes — the review gate applies to a positive acceptance
only (missing/malformed record -> review_missing), a fail verdict never
blocks a rejection/return-for-revision, and identity comparison uses the
full account after the role prefix — therefore must hold in the COPY as
well. This module:

(a) asserts the two guard files (``mcp/lifecycle/review.py``,
    ``mcp/project/flow.py``) stay byte-identical between the trees;
(b) re-runs the blocker regression cases against the copy's own
    ``mcp/server.py`` — loaded in this suite's own process (this file is
    the only one here that imports ``mcp``), so the ``mcp`` package
    resolves to the ``agentteams-teamharness`` tree.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


MCP_DIR = Path(__file__).resolve().parents[2] / "agentteams-teamharness" / "mcp"
LEGACY_MCP_DIR = Path(__file__).resolve().parents[2] / "teamharness" / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import server  # noqa: E402


def _load_server() -> Any:
    """This file's own copy of the bundle tree's server (suite
    convention, see plugins/tests/teamharness/test_protocol_guard.py):
    refs name resolution is process-global and every harness that execs
    server.py rebinds it at exec time — so this file's monkeypatches
    must land on the copy the proxies resolve against, i.e. the copy
    this file loads itself."""
    spec = importlib.util.spec_from_file_location(
        "teamharness_mcp_server_protocol_guard_parity_test",
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


_own_copy_loaded = False


@pytest.fixture(autouse=True)
def bound_server() -> None:
    """Drive this file's own server copy (loaded once; no other test
    file of this suite runs between the tests of this file, so the
    binding holds for all of them)."""
    global server, _own_copy_loaded
    if not _own_copy_loaded:
        server = _load_server()
        _own_copy_loaded = True


WORKER_A = "@alice:example.test"
ROOM = "!taskroom:example.test"

PROJECT_ID = "w2p-project"
TASK_ID = "w2p-task-01"

SUBMISSION_ID = "sub-w2p-0001"


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = server.call_tool(tool, arguments)
    return json.loads(response["content"][0]["text"])


def _enforce_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMHARNESS_PROTOCOL_ENFORCE", "1")


def _write_project(
    workspace: Path,
    *,
    task_status: str = "planned",
    project_id: str = PROJECT_ID,
    task_id: str = TASK_ID,
    assignee: str = WORKER_A,
) -> dict[str, Any]:
    project = {
        "project_id": project_id,
        "title": "W2 parity project",
        "status": "active",
        "tasks": [
            {
                "task_id": task_id,
                "title": "W2 parity task",
                "assigned_to": assignee,
                "depends_on": [],
                "status": task_status,
            }
        ],
        "requester_report": {"pending": False, "sent_at": "2026-10-29T00:00:00Z"},
    }
    server._write_json(workspace / "shared" / "projects" / project_id / "meta.json", project)
    return project


def _write_task_meta(workspace: Path, task: dict[str, Any]) -> None:
    server._write_json(
        workspace / "shared" / "tasks" / task["task_id"] / "meta.json", task
    )


def _task_meta(workspace: Path, task_id: str = TASK_ID) -> dict[str, Any]:
    return server._read_json(workspace / "shared" / "tasks" / task_id / "meta.json")


def _write_submitted_task(
    workspace: Path,
    *,
    task_id: str = TASK_ID,
    project_id: str = PROJECT_ID,
    **extra: Any,
) -> dict[str, Any]:
    """A submitted task meta in the exact shape submit_task persists
    (result fence + submission identity); extra fields are layered on top."""
    _write_project(workspace, task_status="submitted", task_id=task_id, project_id=project_id)
    task: dict[str, Any] = {
        "task_id": task_id,
        "project_id": project_id,
        "status": "submitted",
        "result_status": "SUCCESS",
        "summary": "done",
        "deliverables": [],
        "submission_id": SUBMISSION_ID,
        "submitted_at": "2026-10-29T00:00:00Z",
        "continuation": {"status": "pending", "delivery_id": "dl-w2p-0001"},
    }
    task.update(extra)
    _write_task_meta(workspace, task)
    return task


def _submit(
    workspace: Path,
    *,
    role: str = "worker",
    account: str | None = None,
    task_id: str = TASK_ID,
    project_id: str = PROJECT_ID,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "role": role,
        "action": "submit_task",
        "workspaceDir": str(workspace),
        "payload": {
            "projectId": project_id,
            "taskId": task_id,
            "status": "SUCCESS",
            "summary": "done",
            "deliverables": [],
        },
    }
    if account is not None:
        arguments["account_id"] = account
    return _call("taskflow", arguments)


def _review(
    workspace: Path,
    *,
    role: str = "critic",
    account: str | None = None,
    verdict: str = "pass",
    task_id: str = TASK_ID,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "role": role,
        "action": "review_task",
        "workspaceDir": str(workspace),
        "payload": {
            "taskId": task_id,
            "verdict": verdict,
            "note": "looks right",
        },
    }
    if account is not None:
        arguments["account_id"] = account
    return _call("taskflow", arguments)


def _accept(
    workspace: Path,
    *,
    accepted: bool = True,
    task_id: str = TASK_ID,
    project_id: str = PROJECT_ID,
    submission_id: str = SUBMISSION_ID,
) -> dict[str, Any]:
    return _call("projectflow", {
        "role": "leader",
        "action": "accept_task_result",
        "workspaceDir": str(workspace),
        "payload": {
            "projectId": project_id,
            "taskId": task_id,
            "submissionId": submission_id,
            "resultStatus": "SUCCESS",
            "accepted": accepted,
        },
    })


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The role must come from the call arguments, not the runtime env,
    and the protocol switch from the test itself (default off)."""
    for var in (
        "TEAMHARNESS_PROTOCOL_ENFORCE",
        "TEAMHARNESS_RUNTIME_CONFIG",
        "AGENTTEAMS_AGENT_ROLE",
        "AGENTTEAMS_WORKER_ROLE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shared storage + signal side effects stubbed out: the tests are
    about the protocol layer, not the transport."""
    monkeypatch.setattr(server, "_pull_project", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_pull_task", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_sync_task", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_sync_project", lambda *_a, **_k: True)
    monkeypatch.setattr(server, "_publish_task_artifacts", lambda *_a, **_k: [])
    monkeypatch.setattr(server.taskflow, "_emit_signal_event", lambda *_a, **_k: True)
    monkeypatch.setattr(server.flow, "_emit_signal_event", lambda *_a, **_k: True)


# --- (a) the guard files stay byte-identical between the trees -----------


@pytest.mark.parametrize("relpath", ["lifecycle/review.py", "project/flow.py"])
def test_guard_files_byte_identical_between_trees(relpath: str) -> None:
    legacy = (LEGACY_MCP_DIR / relpath).read_bytes()
    bundle = (MCP_DIR / relpath).read_bytes()
    assert bundle == legacy, (
        f"mcp/{relpath} drifted between plugins/teamharness and "
        "plugins/agentteams-teamharness (re-sync whole per DEDUPE.md)"
    )


# --- (b) the blocker regression cases against the bundle tree ------------


def test_accept_without_review_record_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    """Blocker 1: a submitted r4 task with NO review record is refused
    with review_missing and left 'submitted'."""
    _enforce_on(monkeypatch)
    _write_submitted_task(tmp_path, submitted_by="worker:alice")

    result = _accept(tmp_path)

    assert result["ok"] is False
    assert result["error_code"] == "review_missing"
    assert result["message"] == (
        "accept_task_result requires a review record before accepting a result "
        "(missing or malformed)"
    )
    assert _task_meta(tmp_path)["status"] == "submitted"


def test_accept_with_malformed_review_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    """Blocker 1: a review that is not a verdict-bearing record (a bare
    string, an empty dict, a dict without a verdict) is malformed:
    review_missing, task left 'submitted'."""
    _enforce_on(monkeypatch)
    for malformed in ("fail", {}, {"reviewer": "critic:bob"}):
        _write_submitted_task(
            tmp_path, submitted_by="worker:alice", review=malformed
        )

        result = _accept(tmp_path)

        assert result["ok"] is False
        assert result["error_code"] == "review_missing"
        assert _task_meta(tmp_path)["status"] == "submitted"


def test_accept_with_pass_review_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    """The positive path: a verdict-bearing pass record unblocks the
    acceptance of a submitted r4 result."""
    _enforce_on(monkeypatch)
    _write_submitted_task(
        tmp_path,
        submitted_by="worker:alice",
        review={
            "verdict": "pass",
            "reviewer": "critic:bob",
            "reviewer_team": "",
            "reviewed_at": "2026-10-29T00:00:00Z",
            "note": "looks right",
        },
    )

    result = _accept(tmp_path)

    assert result["ok"] is True
    assert result["nodeStatus"] == "completed"
    assert result["accepted"] is True
    assert _task_meta(tmp_path)["status"] == "completed"


def test_rejection_with_fail_verdict_reaches_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    """Blocker 2: with a fail review record the leader's accepted=false
    still works: node 'revision', no review_not_passed, task NOT left
    'submitted'."""
    _enforce_on(monkeypatch)
    _write_submitted_task(tmp_path, submitted_by="worker:alice")
    reviewed = _review(tmp_path, role="critic", account="bob", verdict="fail")
    assert reviewed["ok"] is True

    result = _accept(tmp_path, accepted=False)

    assert result["ok"] is True
    assert "error_code" not in result
    assert result["nodeStatus"] == "revision"
    assert result["accepted"] is False
    assert _task_meta(tmp_path)["status"] == "revision"


def test_fail_verdict_still_blocks_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    """The other side of Blocker 2: a fail verdict still refuses a
    POSITIVE acceptance (review_not_passed)."""
    _enforce_on(monkeypatch)
    _write_submitted_task(tmp_path, submitted_by="worker:alice")
    reviewed = _review(tmp_path, role="critic", account="bob", verdict="fail")
    assert reviewed["ok"] is True

    result = _accept(tmp_path, accepted=True)

    assert result["ok"] is False
    assert result["error_code"] == "review_not_passed"
    assert _task_meta(tmp_path)["status"] == "submitted"


def test_account_of_strips_only_the_role_prefix() -> None:
    account_of = server.review._account_of
    assert account_of("worker:@alice:example.test") == "@alice:example.test"
    assert account_of("critic:@bob:example.test") == "@bob:example.test"
    assert account_of("remote-member:@carol:example.test") == "@carol:example.test"
    assert account_of("unknown:@dave:example.test") == "@dave:example.test"
    assert account_of("alice") == "alice"
    # An actor whose prefix is not a known role is returned unchanged —
    # never split on the last colon.
    assert account_of("observer:@alice:example.test") == "observer:@alice:example.test"


def test_review_colon_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    """Blocker 3: same full account across roles is still self-review;
    a distinct full account (@bob) is allowed — under the old rsplit
    both reduced to 'example.test' and were refused."""
    _enforce_on(monkeypatch)
    _write_project(tmp_path, task_status="assigned")
    _write_task_meta(tmp_path, {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "status": "assigned",
        "assigned_to": WORKER_A,
    })

    submitted = _submit(tmp_path, role="worker", account="@alice:example.test")
    assert submitted["ok"] is True
    assert submitted["task"]["submitted_by"] == "worker:@alice:example.test"

    same = _review(tmp_path, role="critic", account="@alice:example.test", verdict="pass")
    assert same["ok"] is False
    assert same["error_code"] == "self_review_forbidden"
    assert "review" not in _task_meta(tmp_path)

    ok = _review(tmp_path, role="critic", account="@bob:example.test", verdict="pass")
    assert ok["ok"] is True
    review = _task_meta(tmp_path)["review"]
    assert review["reviewer"] == "critic:@bob:example.test"
    assert review["verdict"] == "pass"


def test_review_identical_actor_string_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    """The identical actor string (same role and account) is refused by
    the exact-identity branch of the self-review guard. (A worker cannot
    be the reviewer — the role gate precedes the identity check — so the
    identical-string case is exercised with the critic identity on both
    sides.)"""
    _enforce_on(monkeypatch)
    _write_submitted_task(tmp_path, submitted_by="critic:@alice:example.test")

    result = _review(tmp_path, role="critic", account="@alice:example.test", verdict="pass")

    assert result["ok"] is False
    assert result["error_code"] == "self_review_forbidden"
    assert "review" not in _task_meta(tmp_path)
