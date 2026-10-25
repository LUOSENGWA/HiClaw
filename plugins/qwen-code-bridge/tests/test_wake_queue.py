# -*- coding: utf-8 -*-
"""Durable wake queue: 409 must delay a wake, never drop it."""

import asyncio
import time

import pytest

from qwen_code_bridge import wake_queue


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    queue_file = tmp_path / "wake-queue.json"
    monkeypatch.setattr(wake_queue, "_path", lambda: queue_file)
    wake_queue.stop()
    monkeypatch.setattr(wake_queue, "_on_delivered", None)
    monkeypatch.setattr(wake_queue, "_sender", None)
    yield
    wake_queue.stop()


def _task(n: str) -> str:
    return n * 32


def test_enqueue_persists_and_dedups() -> None:
    wake_queue.enqueue({"task_id": _task("a"), "text": "first", "session_id": "s"})
    wake_queue.enqueue({"task_id": _task("a"), "text": "second", "session_id": "s"})
    records = wake_queue.load()
    assert len(records) == 1
    assert records[0]["text"] == "second"


def test_drain_success_removes_and_calls_callback() -> None:
    delivered: list[str] = []
    calls: list[tuple] = []

    def sender(session_id, agent_id, base_url, text, channel, user_id, bell,
               approval_level=""):
        calls.append((session_id, text, approval_level))

    wake_queue._on_delivered = lambda task_id: delivered.append(task_id)
    wake_queue.enqueue(
        {
            "task_id": _task("b"),
            "text": "done",
            "session_id": "s-1",
            "approval_level": "AUTO",
        }
    )

    stats = asyncio.run(wake_queue.drain_once(sender))

    assert stats == {"sent": 1, "failed": 0, "dropped": 0, "busy": 0}
    assert wake_queue.load() == []
    assert delivered == [_task("b")]
    assert calls and calls[0][2] == "AUTO"


def test_drain_failure_reschedules_with_backoff() -> None:
    def sender(*_args, **_kwargs):
        raise RuntimeError("wake inject failed: http 409")

    wake_queue.enqueue({"task_id": _task("c"), "text": "done", "session_id": "s"})
    before = time.time()

    stats = asyncio.run(wake_queue.drain_once(sender))

    assert stats == {"sent": 0, "failed": 1, "dropped": 0, "busy": 0}
    records = wake_queue.load()
    assert len(records) == 1
    record = records[0]
    assert record["attempts"] == 1
    assert record["next_at"] >= before + wake_queue.BACKOFF_S[0]
    assert "409" in record["last_error"]


def test_drain_drops_after_max_age() -> None:
    wake_queue.enqueue({"task_id": _task("d"), "text": "done", "session_id": "s"})
    records = wake_queue.load()
    records[0]["created_at"] = time.time() - wake_queue.MAX_AGE_S - 60
    wake_queue._save(records)

    called = []
    stats = asyncio.run(
        wake_queue.drain_once(lambda *a, **k: called.append(a))
    )

    assert stats == {"sent": 0, "failed": 0, "dropped": 1, "busy": 0}
    assert wake_queue.load() == []
    assert called == []


def test_discard_removes_only_target() -> None:
    wake_queue.enqueue({"task_id": _task("e"), "text": "x", "session_id": "s"})
    wake_queue.enqueue({"task_id": _task("f"), "text": "y", "session_id": "s"})
    wake_queue.discard(_task("e"))
    assert [r["task_id"] for r in wake_queue.load()] == [_task("f")]

def test_drain_defers_while_chat_busy(monkeypatch) -> None:
    monkeypatch.setattr(wake_queue, "chat_is_busy", lambda *a: True)
    wake_queue.enqueue(
        {
            "task_id": _task("g"),
            "text": "done",
            "session_id": "s",
            "chat_id": "chat-1",
            "base_url": "http://127.0.0.1:1",
        }
    )

    sent = []
    before = time.time()
    stats = asyncio.run(
        wake_queue.drain_once(lambda *a, **k: sent.append(a))
    )

    assert stats == {"sent": 0, "failed": 0, "dropped": 0, "busy": 1}
    assert sent == []
    record = wake_queue.load()[0]
    assert record["attempts"] == 0          # busy is not a failure
    assert record["busy_retries"] == 1
    assert record["next_at"] >= before + wake_queue.BUSY_RETRY_S


def test_chat_is_busy_probe_failure_falls_through(monkeypatch) -> None:
    # No base_url -> never considered busy (POST keeps its 409 guard).
    assert wake_queue.chat_is_busy("", "a", "c") is False
    assert wake_queue.chat_is_busy("http://127.0.0.1:1", "a", "") is False
