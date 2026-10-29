"""W2 protocol guards (issue #1451 W2): decision class, decision_status,
the attention stall chain, and the review gate.

Ten-row matrix from the W2 task spec, plus the compatibility checks
around it:

  1. enforce off, delegate without decision_class -> success (field not written)
  2. enforce on,  delegate without decision_class -> missing_decision_class
  3. enforce on,  delegate with r4               -> success, persisted
  4. r1 task, no resolved approval record        -> accept -> approval_required
  5. r1 task, with a resolved approval record    -> accept -> success
  6. reviewer == submitter (same account)        -> self_review_forbidden
  7. review verdict fail                         -> accept -> review_not_passed
  8. stalls 0/1/2/3                              -> open/open/blocked/frozen
  9. frozen unfreeze without approval            -> refused, approval unfreezes
 10. contracts vs constants drift                -> ids, thresholds, values

Rows 1-3 also assert the off-switch byte identity: with the switch off no
protocol field (decision_class / decision_status / stall_count /
submitted_by) is written at all.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


MCP_DIR = Path(__file__).resolve().parents[2] / "teamharness" / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import server  # noqa: E402

from mcp.common import constants  # noqa: E402


def _load_server() -> Any:
    """This file's own copy of the server (suite convention, see
    test_mcp_workspace / test_pull_project): refs name resolution is
    process-global and every harness that execs server.py rebinds it at
    exec time — so this file's monkeypatches must land on the copy the
    proxies resolve against, i.e. the copy this file loads itself."""
    spec = importlib.util.spec_from_file_location(
        "teamharness_mcp_server_protocol_guard_test",
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
    """Drive this file's own server copy (loaded once; no other test file
    runs between the tests of this file, so the binding holds for all of
    them)."""
    global server, _own_copy_loaded
    if not _own_copy_loaded:
        server = _load_server()
        _own_copy_loaded = True


WORKER_A = "@alice:example.test"
ROOM = "!taskroom:example.test"

PROJECT_ID = "w2-project"
TASK_ID = "w2-task-01"

SUBMISSION_ID = "sub-w2-0001"


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
        "title": "W2 protocol project",
        "status": "active",
        "tasks": [
            {
                "task_id": task_id,
                "title": "W2 task",
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
        "continuation": {"status": "pending", "delivery_id": "dl-w2-0001"},
    }
    task.update(extra)
    _write_task_meta(workspace, task)
    return task


def _delegate(
    workspace: Path,
    *,
    task_id: str = TASK_ID,
    project_id: str = PROJECT_ID,
    assigned_to: str = WORKER_A,
    decision_class: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "projectId": project_id,
        "taskId": task_id,
        "roomId": ROOM,
        "assignedTo": assigned_to,
        "title": "W2 task",
        "spec": "Do the thing.",
    }
    if decision_class is not None:
        payload["decisionClass"] = decision_class
    return _call("taskflow", {
        "role": "leader",
        "action": "delegate_task",
        "workspaceDir": str(workspace),
        "payload": payload,
    })


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
            "accepted": True,
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


@pytest.fixture
def delegate_env(monkeypatch: pytest.MonkeyPatch, storage: None) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"notify": []}

    def notify(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["notify"].append(kwargs)
        return {
            "sent": True,
            "eventId": f"$delegate-{len(calls['notify']) + 1}",
            "roomId": kwargs.get("room_id", ""),
            "assignee": kwargs.get("assignee", ""),
        }

    monkeypatch.setenv("AGENTTEAMS_MATRIX_URL", "http://matrix.example.test")
    monkeypatch.setenv("AGENTTEAMS_WORKER_MATRIX_TOKEN", "test-token")
    monkeypatch.setattr(server, "_send_delegate_notification", notify)
    monkeypatch.setattr(
        server, "_validate_assignee_membership", lambda *_a, **_k: {"ok": True}
    )
    return calls


@pytest.fixture
def attention_env(monkeypatch: pytest.MonkeyPatch, storage: None) -> None:
    monkeypatch.setattr(
        server.taskflow,
        "_send_attention_notification",
        lambda *_a, **_k: {
            "sent": True,
            "eventId": f"$att-{_k.get('kind')}-{_k.get('attempt')}",
        },
    )
    monkeypatch.setattr(server.taskflow, "_team_leader_matrix_id", lambda: "@leader:example.test")
    monkeypatch.setattr(server.taskflow, "_team_human_matrix_ids", lambda: [])
    monkeypatch.setattr(
        server, "_validate_assignee_membership", lambda *_a, **_k: {"ok": True}
    )


def _request_attention(
    workspace: Path,
    *,
    kind: str = "decision",
    question: str = "Need a decision.",
    resolved: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"taskId": TASK_ID, "kind": kind, "question": question}
    if resolved is not None:
        payload["resolved"] = resolved
    return _call("taskflow", {
        "role": "worker",
        "action": "request_attention",
        "workspaceDir": str(workspace),
        "payload": payload,
    })


def _write_in_progress_task(workspace: Path) -> None:
    _write_project(workspace, task_status="in_progress")
    _write_task_meta(workspace, {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "status": "in_progress",
        "room_id": ROOM,
        "assigned_to": WORKER_A,
    })


# --- Row 1: enforce off, delegate without class -> success ---------------


def test_row1_enforce_off_delegate_without_class_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delegate_env: dict[str, list[Any]]
) -> None:
    _write_project(tmp_path)

    result = _delegate(tmp_path)

    assert result["ok"] is True
    assert result["task"]["status"] == "assigned"
    # Off-switch byte identity: no protocol field is written at all.
    task = _task_meta(tmp_path)
    assert "decision_class" not in task
    assert "decision_status" not in task
    # Compat rule: an absent decision class reads as the r4 default.
    assert server.review._decision_class_of(task) == constants.DEFAULT_DECISION_CLASS


# --- Row 2: enforce on, delegate without class -> missing ---------------


def test_row2_enforce_on_missing_class_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delegate_env: dict[str, list[Any]]
) -> None:
    _enforce_on(monkeypatch)
    _write_project(tmp_path)

    result = _delegate(tmp_path)

    assert result["ok"] is False
    assert result["error_code"] == "missing_decision_class"
    assert result["message"] == (
        "decision_class is required (r1..r6, see contracts/decision-classes.json)"
    )
    # No task state is created by the rejected delegation.
    assert not (tmp_path / "shared" / "tasks" / TASK_ID / "meta.json").exists()


# --- Row 3: enforce on, r4 accepted and persisted -------------------------


def test_row3_enforce_on_r4_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delegate_env: dict[str, list[Any]]
) -> None:
    _enforce_on(monkeypatch)
    _write_project(tmp_path)

    result = _delegate(tmp_path, decision_class="r4")

    assert result["ok"] is True
    assert result["task"]["decision_class"] == "r4"
    assert _task_meta(tmp_path)["decision_class"] == "r4"


def test_enforce_on_unknown_class_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delegate_env: dict[str, list[Any]]
) -> None:
    _enforce_on(monkeypatch)
    _write_project(tmp_path)

    result = _delegate(tmp_path, decision_class="r9")

    assert result["ok"] is False
    assert result["error_code"] == "unknown_decision_class"
    assert result["message"] == (
        "decision_class must be one of r1, r2, r3, r4, r5, r6 "
        "(see contracts/decision-classes.json)"
    )


def test_config_file_switch_and_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delegate_env: dict[str, list[Any]]
) -> None:
    # Switch on via the runtime config file (protocol.enforce: true).
    config_path = tmp_path / "runtime-config.json"
    config_path.write_text(
        json.dumps({"protocol": {"enforce": True}}), encoding="utf-8"
    )
    monkeypatch.setenv("TEAMHARNESS_RUNTIME_CONFIG", str(config_path))
    _write_project(tmp_path)
    result = _delegate(tmp_path)
    assert result["ok"] is False
    assert result["error_code"] == "missing_decision_class"

    # Env override "0" wins over the config file: the guard goes back off.
    monkeypatch.setenv("TEAMHARNESS_PROTOCOL_ENFORCE", "0")
    _write_project(tmp_path, task_id="w2-task-02")
    result = _delegate(tmp_path, task_id="w2-task-02")
    assert result["ok"] is True
    assert "decision_class" not in _task_meta(tmp_path, "w2-task-02")


# --- Row 4: r1 without approval record -> approval_required --------------


def test_row4_r1_without_approval_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    _enforce_on(monkeypatch)
    _write_submitted_task(tmp_path, decision_class="r1")

    result = _accept(tmp_path)

    assert result["ok"] is False
    assert result["error_code"] == "approval_required"
    assert result["message"] == (
        "r1 task result requires a resolved approval record "
        "before it can be decided"
    )


# --- Row 5: r1 with a resolved approval record -> accepted ---------------


def test_row5_r1_with_approval_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    _enforce_on(monkeypatch)
    _write_submitted_task(
        tmp_path,
        decision_class="r1",
        attention=[
            {
                "kind": "approval",
                "question": "Approve the result?",
                "attempt": 1,
                "resolved": True,
                "eventId": "$att-approval-1",
                "stall_count": 0,
            }
        ],
    )

    result = _accept(tmp_path)

    assert result["ok"] is True
    assert result["nodeStatus"] == "completed"
    assert result["accepted"] is True


# --- Row 6: self review (same account, different role) --------------------


def test_row6_same_account_review_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    _enforce_on(monkeypatch)
    _write_project(tmp_path, task_status="assigned")
    _write_task_meta(tmp_path, {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "status": "assigned",
        "assigned_to": WORKER_A,
    })

    submitted = _submit(tmp_path, role="worker", account="alice")
    assert submitted["ok"] is True
    assert submitted["task"]["submitted_by"] == "worker:alice"

    # Same account as the submitter, different role: still self-review.
    result = _review(tmp_path, role="leader", account="alice", verdict="pass")
    assert result["ok"] is False
    assert result["error_code"] == "self_review_forbidden"
    assert result["message"] == "reviewer must differ from submitted_by"
    assert "review" not in _task_meta(tmp_path)

    # A different account is allowed and the record is persisted.
    ok = _review(tmp_path, role="leader", account="bob", verdict="pass")
    assert ok["ok"] is True
    review = _task_meta(tmp_path)["review"]
    assert review["reviewer"] == "leader:bob"
    assert review["verdict"] == "pass"


# --- Row 7: verdict fail -> accept refused --------------------------------


def test_row7_fail_verdict_blocks_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    _enforce_on(monkeypatch)
    _write_submitted_task(tmp_path, submitted_by="worker:alice")
    reviewed = _review(tmp_path, role="critic", account="bob", verdict="fail")
    assert reviewed["ok"] is True

    result = _accept(tmp_path)

    assert result["ok"] is False
    assert result["error_code"] == "review_not_passed"
    assert result["message"] == (
        "accept_task_result requires a passing review verdict "
        "(current verdict: 'fail')"
    )


def test_enforce_off_fail_verdict_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    # Switch off: the pre-W2 behaviour, a fail review is advisory only.
    _write_submitted_task(
        tmp_path,
        submitted_by="worker:alice",
        review={
            "verdict": "fail",
            "reviewer": "leader:bob",
            "reviewer_team": "",
            "reviewed_at": "2026-10-29T00:00:00Z",
            "note": "nope",
        },
    )

    result = _accept(tmp_path)

    assert result["ok"] is True
    assert result["nodeStatus"] == "completed"


# --- Row 8: the stall chain 0/1/2/3 ---------------------------------------


def test_row8_stall_chain_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    storage: None, attention_env: None,
) -> None:
    _enforce_on(monkeypatch)
    _write_in_progress_task(tmp_path)

    created = _request_attention(tmp_path)
    assert created["ok"] is True
    record = created["task"]["attention"][0]
    assert record["stall_count"] == 0
    assert record["resolved"] is False
    assert record.get("eventId")
    assert created["task"].get("decision_status", constants.DEFAULT_DECISION_STATUS) == "open"

    bumped = _request_attention(tmp_path)
    assert bumped["ok"] is True
    assert bumped["attention"]["reused"] is True
    task = bumped["task"]
    assert len(task["attention"]) == 1, "reuse path must not append a second record"
    assert task["attention"][0]["stall_count"] == 1
    assert task.get("decision_status", constants.DEFAULT_DECISION_STATUS) == "open"
    assert "decision_status_history" not in task

    blocked = _request_attention(tmp_path)
    task = blocked["task"]
    assert task["attention"][0]["stall_count"] == constants.ATTENTION_STALL_BLOCKED
    assert task["decision_status"] == "blocked"
    history = task["decision_status_history"]
    assert len(history) == 1
    assert history[0] == {
        "ts": history[0]["ts"],
        "from": "open",
        "to": "blocked",
        "reason": "attention stalled 2 times without an answer",
        "source": "attention:decision",
    }

    frozen = _request_attention(tmp_path)
    task = frozen["task"]
    assert task["attention"][0]["stall_count"] == constants.ATTENTION_STALL_FROZEN
    assert task["decision_status"] == "frozen"
    history = task["decision_status_history"]
    assert [(h["from"], h["to"]) for h in history] == [("open", "blocked"), ("blocked", "frozen")]
    assert history[1]["reason"] == "attention stalled 3 times without an answer"
    # The persisted meta agrees (the chain writes through).
    assert _task_meta(tmp_path)["decision_status"] == "frozen"


# --- Row 9: only a resolved approval record unfreezes ---------------------


def test_row9_frozen_unfreeze_requires_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    storage: None, attention_env: None,
) -> None:
    _enforce_on(monkeypatch)
    _write_in_progress_task(tmp_path)

    # Escalate the decision loop to frozen: create + 3 unanswered re-requests.
    _request_attention(tmp_path)
    _request_attention(tmp_path)
    _request_attention(tmp_path)
    frozen = _request_attention(tmp_path)
    assert frozen["task"]["decision_status"] == "frozen"

    # A non-approval answer cannot unfreeze a frozen task.
    refused = _request_attention(tmp_path, kind="decision", resolved=True)
    assert refused["ok"] is False
    assert refused["error_code"] == "frozen_unfreeze_requires_approval"
    assert refused["message"] == (
        "task is frozen; only a resolved approval record (kind=approval) "
        "can unfreeze it"
    )
    task = _task_meta(tmp_path)
    assert task["decision_status"] == "frozen"
    assert task["attention"][0]["resolved"] is False

    # Open the approval channel and answer it: the task thaws.
    opened = _request_attention(tmp_path, kind="approval", question="Approve?")
    assert opened["ok"] is True
    answered = _request_attention(tmp_path, kind="approval", resolved=True)
    assert answered["ok"] is True
    task = answered["task"]
    assert task["decision_status"] == "open"
    approval = next(r for r in task["attention"] if r["kind"] == "approval")
    assert approval["resolved"] is True
    assert approval["stall_count"] == 0
    history = task["decision_status_history"]
    assert history[-1]["to"] == "open"
    assert history[-1]["reason"] == "unfrozen by approval record"
    assert history[-1]["source"] == "attention:approval"


# --- Row 10: contracts and constants must not drift -----------------------


def test_row10_contracts_match_constants() -> None:
    contracts_dir = MCP_DIR.parent / "contracts"

    classes = json.loads(
        (contracts_dir / "decision-classes.json").read_text(encoding="utf-8")
    )
    ids = [str(item.get("id") or "") for item in classes["classes"]]
    assert ids == list(constants.DECISION_CLASS_IDS), (
        "decision-classes.json class ids (order) drifted from DECISION_CLASS_IDS"
    )
    assert set(ids) == set(constants.DECISION_CLASS_IDS)

    attention = json.loads(
        (contracts_dir / "attention.json").read_text(encoding="utf-8")
    )
    assert attention["stall_thresholds"]["blocked"] == constants.ATTENTION_STALL_BLOCKED
    assert attention["stall_thresholds"]["frozen"] == constants.ATTENTION_STALL_FROZEN
    assert attention["decision_status"]["default"] == constants.DEFAULT_DECISION_STATUS
    assert list(attention["decision_status"]["values"]) == list(
        constants.DECISION_STATUS_VALUES
    )
    assert attention["switch"]["default"] is False


# --- review_task gate edges -------------------------------------------------


def test_review_requires_critic_or_leader(
    tmp_path: Path, storage: None
) -> None:
    _write_submitted_task(tmp_path)

    result = _review(tmp_path, role="worker", verdict="pass")

    assert result["ok"] is False
    assert result["error"] == "review_task requires critic or leader role"
    assert "review" not in _task_meta(tmp_path)


def test_review_requires_submitted_state(
    tmp_path: Path, storage: None
) -> None:
    _write_in_progress_task(tmp_path)

    result = _review(tmp_path, role="critic", verdict="pass")

    assert result["ok"] is False
    assert result["error"] == "illegal_transition"
    assert "task is 'in_progress'" in result["detail"]


def test_review_invalid_verdict_rejected(
    tmp_path: Path, storage: None
) -> None:
    _write_submitted_task(tmp_path)

    result = _review(tmp_path, role="critic", verdict="meh")

    assert result["ok"] is False
    assert result["error"] == "verdict must be one of pass, fail"


# --- off-switch byte identity for submit and attention ---------------------


def test_submit_writes_submitted_by_only_when_enforcing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    _enforce_on(monkeypatch)
    _write_project(tmp_path, task_status="assigned")
    _write_task_meta(tmp_path, {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "status": "assigned",
        "assigned_to": WORKER_A,
    })

    result = _submit(tmp_path, role="worker", account="alice")
    assert result["ok"] is True
    assert _task_meta(tmp_path)["submitted_by"] == "worker:alice"


def test_submit_without_enforce_writes_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    _write_project(tmp_path, task_status="assigned")
    _write_task_meta(tmp_path, {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "status": "assigned",
        "assigned_to": WORKER_A,
    })

    result = _submit(tmp_path, role="worker", account="alice")
    assert result["ok"] is True
    task = _task_meta(tmp_path)
    assert "submitted_by" not in task
    assert task["status"] == "submitted"


def test_attention_without_enforce_writes_no_protocol_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    storage: None, attention_env: None,
) -> None:
    _write_in_progress_task(tmp_path)

    created = _request_attention(tmp_path)
    assert created["ok"] is True
    assert "stall_count" not in created["task"]["attention"][0]
    assert "decision_status" not in created["task"]

    reused = _request_attention(tmp_path)
    assert reused["attention"]["reused"] is True
    assert "stall_count" not in reused["task"]["attention"][0]
    assert "decision_status" not in reused["task"]
