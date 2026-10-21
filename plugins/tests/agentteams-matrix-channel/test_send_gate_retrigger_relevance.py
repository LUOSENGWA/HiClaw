"""Send-gate retrigger relevance check (#1386 P1).

A turn's pending reply obligation must only be voided by room events that
can plausibly change it.  Unrelated peer traffic (a parallel worker
answering its own question) must NOT re-trigger the turn: the completed
draft is delivered as-is (plus a "new message(s) arrived" note) instead of
being re-run into a NO_REPLY that silently loses the answer (issue #1386
case A).

Relevant events that still re-trigger:

  * a new message from the turn's trigger sender (the asker follows up),
  * any message that addresses this worker by name.

Run:  pytest plugins/tests/agentteams-matrix-channel/test_send_gate_retrigger_relevance.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("nio")

REPO_ROOT = Path(__file__).resolve().parents[3]
CHANNEL_MODULE_PATH = (
    REPO_ROOT
    / "plugins"
    / "agentteams-matrix-channel"
    / "agentteams_matrix"
    / "channel.py"
)

_spec = importlib.util.spec_from_file_location(
    "agentteams_matrix_channel_1386_relevance", CHANNEL_MODULE_PATH
)
assert _spec is not None and _spec.loader is not None
channel_module = importlib.util.module_from_spec(_spec)
sys.modules["agentteams_matrix_channel_1386_relevance"] = channel_module
_spec.loader.exec_module(channel_module)

AgentTeamsMatrixChannel = channel_module.AgentTeamsMatrixChannel
HistoryEntry = channel_module.HistoryEntry

OWN_ROOT = channel_module._MATRIX_OWN_THREAD_ROOT_KEY
META_ROOT = channel_module._THREAD_META_ROOT_KEY
PLACEHOLDER = channel_module._MATRIX_PLACEHOLDER_THREAD_ROOT_KEY
STREAMING_FINAL = channel_module._MATRIX_STREAMING_FINAL_TEXT_KEY

ROOM = "!room:matrix.local"
WORKER = "@test1:matrix.local"
PEER = "@test2:matrix.local"
LEADER = "@pi-leader:matrix.local"

ORIGINAL_DRAFT = "AT-R02-test1 complete answer (original draft)"


def _make_channel() -> AgentTeamsMatrixChannel:
    return AgentTeamsMatrixChannel(
        process=lambda *args, **kw: None,
        share_session_in_group=True,
    )


def _placeholder_turn_meta() -> dict:
    return {
        "room_id": ROOM,
        "is_dm": False,
        "is_group": True,
        "worker_name": "test1",
        "event_id": "evt-turn1",
        "thread_root_event_id": "evt-turn1",
        "sender_id": LEADER,  # the asker
        "send_gate_snapshot_gen": 0,
        "send_gate_snapshot_records": 0,
        OWN_ROOT: "root-placeholder",
        META_ROOT: "root-placeholder",
        PLACEHOLDER: True,
        STREAMING_FINAL: ORIGINAL_DRAFT,
    }


def _land(channel: AgentTeamsMatrixChannel, sender: str, body: str) -> None:
    channel._record_history(ROOM, HistoryEntry(sender=sender, body=body))


class _PatchCtx:
    def __init__(self, channel: AgentTeamsMatrixChannel):
        self.channel = channel
        self._patchers: list = []
        self.sinks: dict = {}

    def __enter__(self):
        c = self.channel
        self.sinks = {
            "enqueue": AsyncMock(),
            "edit_root": AsyncMock(),
            "send": AsyncMock(),
            "send_content": AsyncMock(),
            "plain_text": AsyncMock(),
            "typing": AsyncMock(),
            "base_completed": AsyncMock(),
        }
        targets = [
            (c, "_enqueue", "enqueue"),
            (c, "_edit_thread_root", "edit_root"),
            (c, "send", "send"),
            (c, "send_message_content", "send_content"),
            (c, "_send_plain_text", "plain_text"),
            (c, "_send_typing", "typing"),
            (channel_module.BaseChannel, "_on_process_completed", "base_completed"),
        ]
        for target, name, key in targets:
            p = patch.object(target, name, self.sinks[key])
            p.start()
            self._patchers.append(p)
        return self.sinks

    def __exit__(self, *exc):
        while self._patchers:
            self._patchers.pop().stop()
        return False


def _complete(channel: AgentTeamsMatrixChannel, meta: dict) -> None:
    asyncio.run(channel._on_process_completed(None, ROOM, meta))


def test_irrelevant_peer_reply_delivers_draft_not_retrigger():
    """Case A: peer's parallel reply must not void the pending answer."""
    channel = _make_channel()
    channel._user_id = WORKER
    _land(channel, PEER, "AT-R02-test2 peer answer")
    with _PatchCtx(channel) as sinks:
        _complete(channel, _placeholder_turn_meta())

    # No re-trigger at all: the original draft is delivered as-is.
    assert not sinks["enqueue"].called
    assert sinks["edit_root"].await_count == 1
    assert sinks["edit_root"].await_args.args[2] == ORIGINAL_DRAFT
    assert not sinks["send"].called
    # ... plus the "new messages arrived" note.
    assert sinks["plain_text"].call_count == 1


def test_trigger_sender_followup_still_retriggers():
    """The asker following up is the legitimate retrigger case."""
    channel = _make_channel()
    channel._user_id = WORKER
    _land(channel, LEADER, "actually, please also cover edge case X")
    with _PatchCtx(channel) as sinks:
        _complete(channel, _placeholder_turn_meta())

    assert sinks["enqueue"].called
    retrigger_meta = sinks["enqueue"].call_args.args[0]["meta"]
    assert retrigger_meta.get(META_ROOT) == "root-placeholder"


def test_peer_message_mentioning_worker_still_retriggers():
    """Someone addressing this worker by name is relevant."""
    channel = _make_channel()
    channel._user_id = WORKER
    _land(channel, PEER, f"hold on {WORKER} the requirement changed")
    with _PatchCtx(channel) as sinks:
        _complete(channel, _placeholder_turn_meta())

    assert sinks["enqueue"].called


def test_mixed_events_one_relevant_retriggers():
    """Irrelevant peer traffic + one relevant event => still retrigger."""
    channel = _make_channel()
    channel._user_id = WORKER
    _land(channel, PEER, "unrelated chatter")
    _land(channel, LEADER, "follow-up question")
    with _PatchCtx(channel) as sinks:
        _complete(channel, _placeholder_turn_meta())

    assert sinks["enqueue"].called


def test_irrelevant_reply_without_placeholder_sends_draft():
    """Non-placeholder turn: irrelevant peer traffic => normal send path."""
    channel = _make_channel()
    channel._user_id = WORKER
    _land(channel, PEER, "unrelated chatter")
    meta = _placeholder_turn_meta()
    del meta[PLACEHOLDER]
    with _PatchCtx(channel) as sinks:
        _complete(channel, meta)

    assert not sinks["enqueue"].called
    assert sinks["send"].await_count == 1
    assert sinks["send"].await_args.args[1] == ORIGINAL_DRAFT
    assert sinks["plain_text"].call_count == 1
