# W2 review gate and protocol guards (issue #1451 W2): the review_task
# record, the accept_task_result acceptance gates, and the structured
# rejection type shared by the taskflow / projectflow dispatch layers.
from __future__ import annotations

from typing import Any

from mcp.common import refs
from mcp.common.config import _protocol_enforce_enabled, _runtime_team_name
from mcp.common.constants import DECISION_CLASS_IDS, DEFAULT_DECISION_CLASS
from mcp.common.io import _read_json, _utc_timestamp
from mcp.common.payload import _first_text, _safe_id
from mcp.common.paths import _task_state_path
from .transitions import _reject_task_transition, _require_task_mutable, _transition_actor

# Call-time lookups (common.refs): these names resolve against the bound
# mcp/server.py namespace, so test monkeypatches of server attributes stay
# visible to this module.
_write_task = refs.proxy("_write_task")
_sync_task = refs.proxy("_sync_task")


class ProtocolGuardError(Exception):
    """Structured W2 protocol rejection: a stable error_code plus the
    stable message. Deliberately NOT a ValueError subclass so the
    dispatch layers return the {ok, error_code, message} surface instead
    of a bare error string."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _decision_class_of(task: dict[str, Any]) -> str:
    """Read the task's decision class; an absent field reads as the r4
    default (compat rule: pre-W2 tasks have no decision class)."""
    return _first_text(task.get("decision_class"), task.get("decisionClass")) or DEFAULT_DECISION_CLASS


def _has_approval_record(task: dict[str, Any]) -> bool:
    """An approval record is a RESOLVED attention record of kind
    'approval' — the existing attention mechanism is the approval channel
    (no parallel mechanism is introduced)."""
    attention = task.get("attention")
    if not isinstance(attention, list):
        return False
    for record in attention:
        if (
            isinstance(record, dict)
            and str(record.get("kind") or "") == "approval"
            and record.get("resolved")
        ):
            return True
    return False


def _guard_delegate_decision_class(payload: dict[str, Any]) -> str:
    """W2: with the protocol switch on, delegate_task requires a known
    decision class (r1..r6, contracts/decision-classes.json). Returns the
    class to persist on the task meta; with the switch off the field is
    ignored entirely (pre-W2 behaviour, byte-for-byte)."""
    if not _protocol_enforce_enabled():
        return ""
    decision_class = _first_text(payload.get("decision_class"), payload.get("decisionClass"))
    if not decision_class:
        raise ProtocolGuardError(
            "missing_decision_class",
            "decision_class is required (r1..r6, see contracts/decision-classes.json)",
        )
    if decision_class not in DECISION_CLASS_IDS:
        raise ProtocolGuardError(
            "unknown_decision_class",
            "decision_class must be one of r1, r2, r3, r4, r5, r6 (see contracts/decision-classes.json)",
        )
    return decision_class


def _protocol_guard_for_accept(task: dict[str, Any], accepted: bool) -> None:
    """W2 acceptance gates for projectflow accept_task_result (skipped
    entirely with the protocol switch off). The gate applies to a
    POSITIVE acceptance only: a rejection / return-for-revision
    (accepted=False) must always be possible, so it returns immediately
    for those. With accepted=True:
    - a missing or malformed review record blocks the acceptance
      (review_missing);
    - a review record with a non-passing verdict blocks the acceptance
      (review_not_passed);
    - an r1 task result (task_result_acceptance class; an absent
      decision_class reads as r4) cannot be decided without a resolved
      approval record (approval_required).
    """
    if not _protocol_enforce_enabled():
        return
    if not accepted:
        return
    review = task.get("review")
    verdict = ""
    if isinstance(review, dict):
        verdict = str(review.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "fail"}:
        raise ProtocolGuardError(
            "review_missing",
            "accept_task_result requires a review record before accepting a result "
            "(missing or malformed)",
        )
    if verdict != "pass":
        raise ProtocolGuardError(
            "review_not_passed",
            f"accept_task_result requires a passing review verdict "
            f"(current verdict: '{review.get('verdict')}')",
        )
    if _decision_class_of(task) == "r1" and not _has_approval_record(task):
        raise ProtocolGuardError(
            "approval_required",
            "r1 task result requires a resolved approval record before it can be decided",
        )


# The role prefix a transition actor can carry (see _transition_actor:
# the canonical _normalize_role output, plus its "unknown" fallback).
# The vocabulary is the union of this tree's role checks — taskflow
# (worker / remote-member / leader), review_task (critic / leader) and
# constants (worker / remote-member). Accounts may themselves contain a
# colon (Matrix identities @user:server), so only a leading known role
# prefix may be stripped — never the last colon segment.
_ACTOR_ROLE_PREFIXES = {
    "leader",
    "worker",
    "critic",
    "remote-member",
    "unknown",
}


def _account_of(actor: str) -> str:
    """The account identity behind a ``role:account`` actor string.

    Strips only a leading known role prefix; everything after the first
    colon is the account, including any further colons. An actor whose
    prefix is not a known role is returned unchanged."""
    text = str(actor)
    prefix, sep, rest = text.partition(":")
    if sep and rest and prefix in _ACTOR_ROLE_PREFIXES:
        return rest
    return text


def _review_task_action(
    arguments: dict[str, Any],
    payload: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    """W2: review_task — the critic/leader review of a submitted task
    result. Only 'submitted' tasks are reviewable; the reviewer must
    differ from the submitting identity (self_review_forbidden). The
    record shape follows the design doc §3.3 review object."""
    if role not in {"critic", "leader"}:
        raise ValueError("review_task requires critic or leader role")
    task_id = _safe_id(payload.get("taskId") or payload.get("task_id"), "taskId")
    task = _read_json(_task_state_path(arguments, task_id))
    if not task:
        raise ValueError("task not found")
    if not task.get("task_id") and task.get("taskId"):
        task["task_id"] = str(task["taskId"])
    _require_task_mutable(arguments, task, task_id, "review_task")
    status = str(task.get("status") or "")
    if status != "submitted":
        _reject_task_transition(
            status,
            status,
            "review_task",
            (
                f"illegal_transition: review_task: task is '{status}'; "
                "wait for submit_task"
            ),
            arguments=arguments,
            task_id=task_id,
        )
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "fail"}:
        raise ValueError("verdict must be one of pass, fail")
    note = str(payload.get("note") or "").strip()
    scorecard = payload.get("scorecard")
    if scorecard is not None and not isinstance(scorecard, dict):
        raise ValueError("scorecard must be an object")
    reviewer = _transition_actor(arguments)
    submitted_by = _first_text(task.get("submitted_by"), task.get("submittedBy"))
    if submitted_by and (
        reviewer == submitted_by or _account_of(reviewer) == _account_of(submitted_by)
    ):
        raise ProtocolGuardError(
            "self_review_forbidden",
            "reviewer must differ from submitted_by",
        )
    review: dict[str, Any] = {
        "verdict": verdict,
        "reviewer": reviewer,
        "reviewer_team": _runtime_team_name(),
        "reviewed_at": _utc_timestamp(),
        "note": note,
    }
    if scorecard is not None:
        review["scorecard"] = scorecard
    task["review"] = review
    _write_task(arguments, task)
    synced = _sync_task(arguments, task_id, exclude=["spec.md", "base/"])
    return {
        "ok": True,
        "tool": "taskflow",
        "action": "review_task",
        "task": task,
        "review": review,
        "synced": synced,
    }
