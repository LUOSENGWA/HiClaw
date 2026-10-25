# -*- coding: utf-8 -*-
"""Durable wake queue — a terminal-task wake must never be lost to a 409.

Why this exists
---------------
A dispatch finishing while the agent's session is **busy** (a long
verification turn can run for tens of minutes) makes the wake inject
return HTTP 409.  The inline sender retries 3×2s and then gives up, so
the wake was lost forever — with batch dispatch this is structural, not
an accident (2026-10-25 incident: five terminal wakes dropped).

Design
------
* Every failed wake is **persisted** to ``<state>/wake-queue.json``
  (atomic tmp+rename), deduplicated by ``task_id``.
* A background loop drains due records with exponential backoff
  (5s → 10min cap) for up to ``MAX_AGE_S`` (24h).  A busy session
  simply means "not due yet"; the wake lands on the first idle window.
* Delivery calls the same ``default_wake_sender`` used inline, so the
  payload/approval-level/room-bell semantics are unchanged.
* On success the task file's ``wake_status`` flips to ``sent`` via the
  ``on_delivered`` callback (wired in ``dispatch.mark_wake_delivered``).

All failures here are swallowed and logged — the queue must never break
a dispatch or a request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from .endpoints import app_base
from .state import state_dir

logger = logging.getLogger(__name__)

QUEUE_FILE = "wake-queue.json"
#: Retry delays after each failed attempt (seconds); last value repeats.
BACKOFF_S = (5, 15, 30, 60, 120, 300, 600)
#: Give up (and drop) a wake older than this.
MAX_AGE_S = 24 * 3600.0
#: Loop tick: how often due records are checked.
TICK_S = 5.0
#: While the chat is running we do not attempt the POST at all; re-check
#: this often instead (the run usually ends within minutes).
BUSY_RETRY_S = 15.0

_lock: Optional[asyncio.Lock] = None
_loop_task: Optional[asyncio.Task] = None
_sender: Optional[Callable[..., None]] = None
_on_delivered: Optional[Callable[[str], None]] = None


def _path() -> Path:
    return state_dir() / QUEUE_FILE


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def load() -> list[dict[str, Any]]:
    """Read the queue (never raises)."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and r.get("task_id")]


def _save(records: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(records, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def enqueue(record: dict[str, Any]) -> None:
    """Persist a failed wake (dedup by task_id) and make sure the loop runs."""
    task_id = str(record.get("task_id") or "").strip()
    if not task_id:
        return
    try:
        now = time.time()
        records = load()
        existing = next(
            (r for r in records if r.get("task_id") == task_id), None
        )
        if existing is None:
            existing = {
                "task_id": task_id,
                "created_at": now,
                "attempts": 0,
                "next_at": now,
            }
            records.append(existing)
        for key, value in record.items():
            if key != "task_id":
                existing[key] = value
        existing.setdefault("created_at", now)
        existing.setdefault("attempts", 0)
        existing.setdefault("next_at", now)
        _save(records)
        logger.info(
            "wake queued: task=%s attempts=%s",
            task_id,
            existing.get("attempts"),
        )
    except Exception as exc:  # queue must never break the caller
        logger.warning("wake enqueue failed: %s", exc)
    ensure_running()


def discard(task_id: str) -> None:
    """Drop a queued wake (e.g. an inline retry finally succeeded)."""
    try:
        tid = str(task_id or "").strip()
        if not tid:
            return
        records = load()
        kept = [r for r in records if r.get("task_id") != tid]
        if len(kept) != len(records):
            _save(kept)
    except Exception as exc:
        logger.warning("wake discard failed: %s", exc)


def pending() -> list[dict[str, Any]]:
    return load()


def chat_is_busy(base_url: str, agent_id: str, chat_id: str) -> bool:
    """True when the chat still has a running turn.

    Mirrors the console frontend's queue semantics
    (``waitForChatIdle`` polls ``GET /api/chats/{id}`` and only sends on
    ``status == "idle"``).  Any probe failure returns False: the POST
    keeps its own atomic ``attach_or_start`` guard (409), so "unknown"
    must fall through rather than delay a wake forever.
    """
    if not chat_id or not base_url:
        return False
    try:
        url = (
            app_base(base_url)
            + "/api/chats/"
            + urllib.parse.quote(str(chat_id), safe="")
            + "/status"
        )
        req = urllib.request.Request(
            url,
            headers={"X-Agent-Id": agent_id or "AgentTeamGuide"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read(4096)
        data = json.loads(raw.decode("utf-8") or "{}")
        status = str((data or {}).get("status") or "").strip().lower()
        return bool(status) and status != "idle"
    except Exception:
        return False


def _due(now: float) -> list[dict[str, Any]]:
    return [
        r for r in load() if float(r.get("next_at") or 0.0) <= now
    ]


def _remove(task_id: str) -> None:
    _save([r for r in load() if r.get("task_id") != task_id])


def _reschedule_busy(record: dict[str, Any], now: float) -> None:
    """Chat still running: retry soon, do not burn the failure backoff."""
    record["busy_retries"] = int(record.get("busy_retries") or 0) + 1
    record["next_at"] = now + BUSY_RETRY_S
    record["last_busy_at"] = now
    records = load()
    for idx, item in enumerate(records):
        if item.get("task_id") == record.get("task_id"):
            records[idx] = record
            break
    else:
        records.append(record)
    _save(records)


def _reschedule(record: dict[str, Any], now: float, error: str) -> None:
    attempts = int(record.get("attempts") or 0) + 1
    delay = BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)]
    record["attempts"] = attempts
    record["last_error"] = str(error or "")[:200]
    record["next_at"] = now + delay
    record["last_attempt_at"] = now
    records = load()
    for idx, item in enumerate(records):
        if item.get("task_id") == record.get("task_id"):
            records[idx] = record
            break
    else:
        records.append(record)
    _save(records)


async def drain_once(sender: Callable[..., None] | None = None) -> dict[str, int]:
    """Try every due record once; returns counters (mostly for tests)."""
    stats = {"sent": 0, "failed": 0, "dropped": 0, "busy": 0}
    now = time.time()
    due = _due(now)
    if not due:
        return stats
    fn = sender or _sender or _default_sender()
    async with _get_lock():
        # Re-read under the lock: another drain may have changed the queue.
        due = _due(time.time())
        for record in due:
            task_id = str(record.get("task_id") or "")
            created = float(record.get("created_at") or 0.0)
            if created and now - created > MAX_AGE_S:
                _remove(task_id)
                stats["dropped"] += 1
                logger.warning("wake dropped after 24h: task=%s", task_id)
                continue
            busy = await asyncio.to_thread(
                chat_is_busy,
                str(record.get("base_url") or ""),
                str(record.get("agent_id") or ""),
                str(record.get("chat_id") or ""),
            )
            if busy:
                _reschedule_busy(record, time.time())
                stats["busy"] += 1
                logger.debug(
                    "wake deferred (chat busy): task=%s busy_retries=%s",
                    task_id,
                    record.get("busy_retries"),
                )
                continue
            try:
                await asyncio.to_thread(
                    fn,
                    str(record.get("session_id") or ""),
                    str(record.get("agent_id") or ""),
                    str(record.get("base_url") or ""),
                    str(record.get("text") or ""),
                    str(record.get("channel") or "console"),
                    str(record.get("user_id") or "default"),
                    bool(record.get("room_bell")),
                    approval_level=str(record.get("approval_level") or ""),
                )
            except Exception as exc:
                _reschedule(record, time.time(), str(exc))
                stats["failed"] += 1
                logger.info(
                    "wake retry failed: task=%s attempts=%s err=%s",
                    task_id,
                    record.get("attempts"),
                    str(exc)[:120],
                )
            else:
                _remove(task_id)
                stats["sent"] += 1
                logger.info("wake delivered from queue: task=%s", task_id)
                if _on_delivered is not None:
                    try:
                        _on_delivered(task_id)
                    except Exception as exc:
                        logger.warning(
                            "wake on_delivered failed: task=%s err=%s",
                            task_id,
                            exc,
                        )
    return stats


async def _loop(sender: Callable[..., None] | None) -> None:
    while True:
        try:
            await drain_once(sender)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the loop die
            logger.warning("wake loop error: %s", exc)
        await asyncio.sleep(TICK_S)


def _default_sender() -> Callable[..., None]:
    from .dispatch import default_wake_sender

    return default_wake_sender


def ensure_running(
    sender: Callable[..., None] | None = None,
    on_delivered: Callable[[str], None] | None = None,
) -> None:
    """Start the drain loop once (no-op when already running / no loop)."""
    global _loop_task, _sender, _on_delivered
    if sender is not None:
        _sender = sender
    if on_delivered is not None:
        _on_delivered = on_delivered
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # sync context: the first async enqueue will start it
    _loop_task = loop.create_task(_loop(_sender))


def stop() -> None:
    """Stop the loop (plugin shutdown / tests)."""
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


__all__ = [
    "BACKOFF_S",
    "MAX_AGE_S",
    "QUEUE_FILE",
    "discard",
    "drain_once",
    "enqueue",
    "ensure_running",
    "load",
    "pending",
    "stop",
]
