# -*- coding: utf-8 -*-
"""Worker-side dispatch surface: gateless headless CLI runs (design §4.2).

Semantics ported (read-only reference) from the workspace dispatcher:

- ``tools/dispatch.sh`` — spawn sequence, state file layout, claim flag;
- ``tools/dispatch_watch.py`` — watchdog loop (20s pid poll, terminal
  state written to file, 3-line terminal summary);
- ``tools/dispatch_notify.py`` — terminal-state wake: session inject
  ``POST /api/console/chat`` with retry on 409/429/503.

Dropped on purpose (10/22 gate-removal ruling): the parallel-limit
gate (``max_parallel``), the ``files_to_touch`` conflict gate, the
budget fuse, and the external claim check (``qc-claim.sh``). The claim
flag is kept: a dispatch with the same task name already ``running``
→ 409. ``files_to_touch`` is accepted by the API schema as pure
metadata only: never persisted, never consumed.

State is file-backed under ``<state_dir>/dispatch/<task_id>.json``
(atomic write, same pattern as install tasks) plus ``<task_id>.log``
(full captured stdout). A stale ``running`` task file (hard crash or
server exit mid-run) blocks re-dispatch of the same task name until
the file is removed manually — same policy as stale install task
files.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from . import approval, chats, millforge, progress, qwen_status, runs
from .endpoints import app_base
from .registry import CLIS
from .state import cli_home, prepare_cli_home, qwen_cli, state_dir

#: Task ids are ``uuid4().hex``; the check also keeps task file names
#: safe against traversal via ``get_task`` / ``get_log`` input.
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Maximum characters of child stdout kept in the task file ``log_tail``.
_LOG_TAIL_LIMIT = 65536

#: Watchdog poll interval (s); same default as dispatch_watch.py.
_DEFAULT_WATCH_INTERVAL = 20.0

#: Env override for the poll interval (tests / ops; dispatch.sh uses
#: DISPATCH_WATCH_INTERVAL for the same purpose).
WATCH_INTERVAL_ENV = "QWENPAW_DISPATCH_WATCH_INTERVAL"

#: Default per-task wall-clock timeout (s).
DEFAULT_DISPATCH_TIMEOUT = 3600.0

_DEFAULT_WAKE_URL = ""  # resolved dynamically via endpoints.app_base
_WAKE_ENDPOINT = "/api/console/chat"
_WAKE_ATTEMPTS = 3

#: Approval tiers accepted by the qwen CLI. Resolution order is 1:1 with
#: ``run-coding-cli.sh`` (#1379): per-run request > env > default "yolo".
APPROVAL_MODES: tuple[str, ...] = (
    "plan",
    "default",
    "auto-edit",
    "auto",
    "yolo",
)

#: Default tier for unattended dispatch (10/22 ruling).
DEFAULT_APPROVAL_MODE = "yolo"

#: Env override mirroring the runner's config fallback (#1379).
APPROVAL_MODE_ENV = "QWEN_CODE_BRIDGE_APPROVAL_MODE"
LEGACY_APPROVAL_MODE_ENV = "AGENTTEAMS_CODING_CLI_APPROVAL_MODE"

#: QwenPaw tool-execution level -> Qwen Code approval tier.
#: STRICT = everything needs approval -> plan (analyze only, never acts);
#: SMART  = low-risk auto -> auto-edit (edits auto, shell still gated);
#: AUTO   = only guarded tools -> auto (classifier blocks risky actions);
#: OFF    = guard disabled -> yolo (everything approved).
APPROVAL_LEVEL_MAP: dict[str, str] = {
    "STRICT": "plan",
    "SMART": "auto-edit",
    "AUTO": "auto",
    "OFF": "yolo",
}


def map_level_to_mode(level: str | None) -> str:
    """Map a QwenPaw approval level to a Qwen Code approval tier."""
    return APPROVAL_LEVEL_MAP.get(str(level or "").strip().upper(), "")

#: Headless spawn args, 1:1 with the production adapter.
#: Authoritative source: ``tools/dispatch_adapters.json`` (entry "qwen",
#: key "args"); ``{approval_mode}`` / ``{sid}`` / ``{prompt}`` are
#: substituted in ``start()``.
SPAWN_ARG_TEMPLATE: tuple[str, ...] = (
    "--approval-mode",
    "{approval_mode}",
    "--channel",
    "CI",
    "-o",
    "stream-json",
    "--chat-recording",
    "--session-id",
    "{sid}",
    "-p",
    "{prompt}",
)

#: Tiers that need a live approver: the CLI is spawned with
#: ``--input-format stream-json`` so the host can answer ``can_use_tool``
#: control requests (worker 代审批). ``yolo``/``plan`` need no approver.
STREAM_INPUT_MODES: tuple[str, ...] = ("default", "auto-edit", "auto")


#: One reaper per dispatch dir (module-level so it survives per-request
#: service instances; the task itself survives a plugin/process restart).
_REAPERS: dict[str, asyncio.Task] = {}


def build_spawn_plan(
    mode: str,
    session_id: str,
    prompt: str,
    max_wall_time: str | None = None,
    max_session_turns: int | None = None,
    allowed_tools: list[str] | None = None,
    resume: bool = False,
) -> tuple[list[str], bool]:
    """Return ``(argv, prompt_via_stdin)`` for one dispatch.

    ``default``/``auto-edit``/``auto`` run in bidirectional stream-json
    mode: no ``-p`` prompt, the first user turn is written to stdin and
    the channel stays open for ``can_use_tool`` control responses.
    ``yolo``/``plan`` keep the byte-compatible one-shot argv.
    """
    extra: list[str] = []
    if max_wall_time:
        extra += ["--max-wall-time", str(max_wall_time)]
    if max_session_turns:
        extra += ["--max-session-turns", str(int(max_session_turns))]
    if allowed_tools:
        extra += ["--allowed-tools", ",".join(str(t) for t in allowed_tools if str(t))]
    session_args = (
        ["--resume", session_id] if resume else ["--session-id", session_id]
    )
    if mode in STREAM_INPUT_MODES:
        return (
            [
                "--approval-mode",
                mode,
                "--channel",
                "CI",
                "-o",
                "stream-json",
                "--chat-recording",
            ]
            + session_args
            + [
                "--input-format",
                "stream-json",
            ]
            + extra,
            True,
        )
    args: list[str] = []
    for item in SPAWN_ARG_TEMPLATE:
        if item == "--session-id":
            # Replaced by session_args (--session-id or --resume).
            continue
        if item == "{sid}":
            args.extend(session_args)
        elif item == "{prompt}":
            args.append(prompt)
        elif item == "{approval_mode}":
            args.append(mode)
        else:
            args.append(item)
    return args + extra, False


def initialize_message(can_use_tool_timeout: int = 600) -> str:
    """SDK-mode handshake: the CLI enables its control system only after
    the host sends this first ``initialize`` control request."""
    return (
        json.dumps(
            {
                "type": "control_request",
                "request_id": uuid.uuid4().hex,
                "request": {
                    "subtype": "initialize",
                    "hooks": None,
                    "timeout": {"canUseTool": can_use_tool_timeout},
                },
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def user_message(session_id: str, prompt: str) -> str:
    """The initial stream-json user turn (one line, newline-terminated)."""
    return (
        json.dumps(
            {
                "type": "user",
                "session_id": session_id,
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def keep_running_after_timeout(
    busy: bool, progress_age: float, stall_after: float
) -> bool:
    """A wall-clock timeout must not kill a session that is still working.

    Keep running while QwenCode reports the session busy **and** the event
    stream has produced something within ``stall_after`` seconds. An idle
    zombie or a session with no progress for the stall window is killed.
    """
    return bool(busy) and float(progress_age) <= float(stall_after)


def chat_sync_enabled() -> bool:
    """Sidebar chat sync switch: env > state/config.json > default true."""
    if os.environ.get("QWEN_CODE_BRIDGE_CHAT_SYNC", "1") == "0":
        return False
    try:
        cfg = state_dir() / "config.json"
        if cfg.exists():
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "chat_sync" in data:
                return bool(data.get("chat_sync"))
    except (OSError, ValueError):
        pass
    return True


def resolve_approval_mode(
    requested: str | None = None,
    level: str | None = None,
) -> str:
    """Resolve the per-run approval tier (flag > env > default "yolo").

    Mirrors ``run-coding-cli.sh`` (#1379). Raises ``ValueError`` on an
    unknown tier so the API answers 400 instead of spawning a CLI with
    an invalid flag.
    """
    for raw in (
        requested,
        os.environ.get(APPROVAL_MODE_ENV, ""),
        os.environ.get(LEGACY_APPROVAL_MODE_ENV, ""),
    ):
        value = str(raw or "").strip()
        if not value:
            continue
        if value not in APPROVAL_MODES:
            raise ValueError(
                f"invalid approval_mode {value!r}; expected one of "
                + ", ".join(APPROVAL_MODES),
            )
        return value
    mapped = map_level_to_mode(level)
    if mapped:
        return mapped
    return DEFAULT_APPROVAL_MODE


class DispatchBusyError(RuntimeError):
    """Raised when a dispatch with the same task name is already running."""


class CliNotFoundError(RuntimeError):
    """Raised when the requested CLI binary is not on PATH."""


class SpawnFailedError(RuntimeError):
    """Raised when the child process cannot be started."""


#: ``wake_sender(session_id, agent_id, base_url, text) -> None``;
#: returns on success, raises on final failure.
WakeSender = Callable[..., None]


def load_task(task_id: str) -> dict[str, Any] | None:
    """Read one dispatch task file (read-only; used by the run-chat view)."""
    if not _TASK_ID_RE.match(task_id or ""):
        return None
    try:
        path = state_dir() / "dispatch" / f"{task_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _effective_approval_level(agent_id: str) -> str:
    """Read the effective QwenPaw tool-execution level for the wake turn.

    Returns ``""`` when the host config is unavailable; the wake then
    omits ``request_context`` and the session default applies (never
    fail the wake over this).
    """
    try:
        from qwenpaw.config.config import load_agent_config
        from qwenpaw.config.utils import load_config

        aid = str(agent_id or "").strip()
        if not aid:
            aid = str(load_config().agents.active_agent or "default")
        profile = load_agent_config(aid)
        return str(getattr(profile, "approval_level", "") or "").strip()
    except Exception:  # pragma: no cover - host config always present
        return ""


def _pending_approval_line(base_url: str, session_id: str) -> str:
    """One compact line describing the newest pending approval ("" if none).

    Reads the host approval list; ``exact_target`` is the literal operation
    (e.g. the shell command) and ``reasoning`` is why the guard flagged it.
    Resolved approvals are not queryable — the service keeps no history.
    """
    root = app_base(base_url)
    url = (
        f"{root}/api/approval/list?session_id="
        + urllib.parse.quote(session_id, safe="")
    )
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return ""
    items = [
        item
        for item in ((data or {}).get("pending_approvals") or [])
        if isinstance(item, dict)
    ]
    if not items:
        return ""
    items.sort(key=lambda x: float(x.get("created_at") or 0.0), reverse=True)
    newest = items[0]
    tool = str(
        newest.get("tool_display_name") or newest.get("tool_name") or "?"
    )
    target = str(newest.get("exact_target") or "")[:80]
    try:
        age = int(max(0.0, time.time() - float(newest.get("created_at") or 0.0)))
    except (TypeError, ValueError):
        age = -1
    sev = str(newest.get("severity") or "")
    line = f"待审批 {len(items)} 条 | 最近: {tool}"
    if target:
        line += f" → {target}"
    line += f" (age {age}s"
    if sev:
        line += f", {sev}"
    line += ")"
    reason = str(newest.get("reasoning") or "")[:60]
    if reason:
        line += f" reason={reason}"
    if len(items) > 1:
        others = ",".join(
            str(i.get("tool_name") or "?") for i in items[1:4]
        )
        line += f"; 其余 {len(items) - 1} 条: {others}"
    return line


def _wake_body(
    session_id: str,
    text: str,
    approval_level: str,
    channel: str = "console",
    user_id: str = "default",
) -> dict:
    """Chat payload; ``channel`` picks console vs matrix, ``request_context``
    pins the wake turn's approval level."""
    body: dict[str, Any] = {
        "channel": str(channel or "console"),
        "user_id": str(user_id or "default"),
        "session_id": session_id,
        "input": [
            {"content": [{"type": "text", "text": text}], "metadata": {}},
        ],
    }
    if approval_level:
        body["request_context"] = {"approval_level": approval_level}
    return body


def _send_room_bell(
    root: str,
    channel: str,
    session_id: str,
    user_id: str,
    text: str,
    agent_id: str,
) -> None:
    """Post the terminal summary to the room via POST /api/messages/send."""
    payload = {
        "channel": channel,
        "target_user": user_id,
        "target_session": session_id,
        "text": text,
    }
    req = urllib.request.Request(
        root + "/api/messages/send",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Agent-Id": agent_id or "AgentTeamGuide",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read(64)


def default_wake_sender(
    session_id: str,
    agent_id: str,
    base_url: str,
    text: str,
    channel: str = "console",
    user_id: str = "default",
    room_bell: bool = False,
) -> None:
    """Real-HTTP session inject (dispatch_notify.py default backend).

    POSTs the terminal summary to ``<base>/api/console/chat`` with the
    ``X-Agent-Id`` header; retries on 409/429/503 and transient errors
    (3 attempts). Raises ``RuntimeError`` on final failure.
    """
    root = app_base(base_url)
    body = _wake_body(
        session_id,
        text,
        _effective_approval_level(agent_id),
        channel,
        user_id,
    )
    pending_line = _pending_approval_line(base_url, session_id)
    if pending_line:
        body["input"][0]["content"][0]["text"] += (
            "\n" + pending_line + "（唤醒回合请先处理）"
        )
    last = ""
    for attempt in range(_WAKE_ATTEMPTS):
        try:
            req = urllib.request.Request(
                root + _WAKE_ENDPOINT,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-Id": agent_id or "AgentTeamGuide",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read(64)
            if room_bell and str(channel or "console") != "console":
                _send_room_bell(
                    root,
                    str(channel),
                    session_id,
                    user_id,
                    text,
                    agent_id,
                )
            return
        except urllib.error.HTTPError as exc:
            last = f"http {exc.code}"
            if exc.code not in (409, 429, 503):
                break
        except Exception as exc:  # wake is best-effort: report, don't crash
            last = str(exc)[:120]
        if attempt + 1 < _WAKE_ATTEMPTS:
            time.sleep(2)
    raise RuntimeError(f"wake inject failed: {last}")


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _install_hint(cli: str) -> str:
    """409 hint text pointing at the install route for managed CLIs."""
    for spec in CLIS.values():
        if spec.bin_name == cli:
            return (
                f"CLI {cli!r} not found on PATH; install it via "
                f"POST /api/qwen-code-bridge/{spec.id}/install"
            )
    return (
        f"CLI {cli!r} not found on PATH; it is not a managed coding "
        "CLI, so no install route exists (install it manually or fix PATH)"
    )


class DispatchService:
    """File-backed dispatch task manager (one instance per request).

    All state lives under ``<state_dir>/dispatch/``; the service holds
    no mutable state beyond immutable configuration, so constructing a
    fresh instance per request is safe (same pattern as
    ``CodingCliService``).
    """

    def __init__(
        self,
        dispatch_dir: Path | None = None,
        watch_interval: float | None = None,
        wake_sender: WakeSender | None = None,
    ) -> None:
        if dispatch_dir is None:
            dispatch_dir = state_dir() / "dispatch"
        self._dispatch_dir = Path(dispatch_dir)
        self._dispatch_dir.mkdir(parents=True, exist_ok=True)
        if watch_interval is None:
            raw = os.environ.get(WATCH_INTERVAL_ENV, "").strip()
            try:
                watch_interval = float(raw) if raw else _DEFAULT_WATCH_INTERVAL
            except ValueError:
                watch_interval = _DEFAULT_WATCH_INTERVAL
        self._watch_interval = max(watch_interval, 0.05)
        self._wake_sender = wake_sender or default_wake_sender

    # ---- state IO (atomic, same pattern as install tasks) ----

    def _task_path(self, task_id: str) -> Path:
        return self._dispatch_dir / f"{task_id}.json"

    def _log_path(self, task_id: str) -> Path:
        return self._dispatch_dir / f"{task_id}.log"

    def _write_task(self, task: dict[str, Any]) -> None:
        path = self._task_path(task["task_id"])
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(task, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _read_task(self, task_id: str) -> dict[str, Any] | None:
        if not _TASK_ID_RE.match(task_id or ""):
            return None
        try:
            return json.loads(
                self._task_path(task_id).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    def _update_task(self, task_id: str, fields: dict[str, Any]) -> None:
        task = self._read_task(task_id)
        if task is not None:
            task.update(fields)
            self._write_task(task)

    # ---- queries ----

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return False
        if value <= 0:
            return False
        try:
            os.kill(value, 0)
            return True
        except OSError:
            return False

    def _mark_stale(self, task: dict[str, Any]) -> dict[str, Any]:
        """A ``running`` task whose pid is gone can never finish by itself.

        The age guard keeps the spawn window (pid not written yet) and
        short-lived test doubles from being reaped; a real crashed task
        stops updating and crosses the threshold.
        """
        age = time.time() - float(
            task.get("started_epoch") or 0.0
        )
        stale_after = float(
            os.environ.get("QWENPAW_DISPATCH_STALE_AFTER", "60") or 60
        )
        if (
            task.get("state") == "running"
            and task.get("pid") is not None
            and age > stale_after
            and not self._pid_alive(task.get("pid"))
        ):
            task.update(
                {
                    "state": "stale",
                    "exit_code": None,
                    "finished_at": task.get("finished_at") or _utcnow(),
                    "updated_at": _utcnow(),
                    "terminal_summary": [
                        f"[{task.get('task')}] stale · 进程已消失（重启/崩溃）",
                        "none: 无终态记录（原 watchdog 已随进程退出）",
                        "下一步：重新派单，或 POST /dispatch/cleanup 清理",
                    ],
                }
            )
            self._write_task(task)
        return task

    def _ensure_reaper(self) -> None:
        """Start the restart-recovery reaper once per dispatch dir.

        The child watchdog dies with the plugin process; after a restart a
        live pid has no watcher and a dead pid has no terminal writer. The
        reaper scans on read and closes both cases (S6).
        """
        key = str(self._dispatch_dir)
        existing = _REAPERS.get(key)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        _REAPERS[key] = loop.create_task(
            self._reaper_loop(), name=f"coding-cli-reaper-{key}"
        )

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self._watch_interval)
            try:
                for task in self._scan_once():
                    await self._notify_external(task)
            except (asyncio.CancelledError, Exception):
                pass

    def _scan_once(self) -> list[dict[str, Any]]:
        """Recover tasks orphaned by a process restart (sync, testable)."""
        changed: list[dict[str, Any]] = []
        for path in sorted(self._dispatch_dir.glob("*.json")):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if task.get("state") != "running":
                continue
            pid = task.get("pid")
            if pid is None:
                continue
            age = time.time() - float(task.get("started_epoch") or 0)
            if not self._pid_alive(pid):
                if age > 60:
                    changed.append(self._mark_stale(task))
                continue
            timeout = float(task.get("timeout") or 0)
            if timeout and age > timeout + 60:
                try:
                    os.kill(int(pid), 9)
                except OSError:
                    pass
                task.update(
                    {
                        "state": "timeout",
                        "exit_code": None,
                        "finished_at": _utcnow(),
                        "updated_at": _utcnow(),
                        "terminal_summary": [
                            f"[{task.get('task')}] timeout · 进程超预算，由重启恢复器终止",
                            "exit_code: unknown (not our child)",
                            "下一步：重新派单或查看日志",
                        ],
                    }
                )
                self._write_task(task)
                changed.append(task)
        return changed

    async def _notify_external(self, task: dict[str, Any]) -> None:
        try:
            if not task.get("run_record"):
                log_text = await asyncio.to_thread(self.get_log, task["task_id"]) or ""
                rec = await asyncio.to_thread(
                    runs.write_run_record, task, log_text, task.get("runs_dir")
                )
                if rec:
                    self._update_task(
                        task["task_id"],
                        {"run_record": rec["record"], "run_log": rec["run"]},
                    )
        except Exception:
            pass
        wake = task.get("wake") or {}
        origin = task.get("origin") or {}
        sid = str(origin.get("session_id") or wake.get("session_id") or "").strip()
        if not sid:
            return
        text = self._wake_text(
            task,
            str(task.get("state") or ""),
            task.get("exit_code"),
            float(task.get("wall_s") or 0),
        )
        try:
            await asyncio.to_thread(
                self._wake_sender,
                sid,
                str(wake.get("agent_id") or ""),
                str(wake.get("base_url") or ""),
                text,
                str(origin.get("channel") or wake.get("channel") or "console"),
                str(origin.get("user_id") or wake.get("user_id") or "default"),
                bool(wake.get("room_bell")),
            )
            self._update_task(task["task_id"], {"wake_status": "sent"})
        except Exception as exc:
            self._update_task(
                task["task_id"],
                {"wake_status": f"failed: {str(exc)[:120]}"},
            )

    def running_task_named(self, task_name: str) -> dict[str, Any] | None:
        """Claim flag: a live ``running`` task file with the same task name."""
        for path in sorted(self._dispatch_dir.glob("*.json")):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if task.get("task") == task_name and task.get("state") == "running":
                return task
        return None

    def cleanup(self, kill: bool = False) -> dict[str, Any]:
        """Mark dead ``running`` tasks as stale (report-first).

        ``kill=True`` only removes nothing live: it is accepted for API
        symmetry; stale task files are kept for audit.
        """
        self._ensure_reaper()
        stale: list[str] = []
        for path in sorted(self._dispatch_dir.glob("*.json")):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if task.get("state") == "running" and not self._pid_alive(task.get("pid")):
                task = self._mark_stale(task)
                stale.append(str(task.get("task_id")))
        return {"stale": stale, "count": len(stale), "kill": bool(kill)}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        self._ensure_reaper()
        task = self._read_task(task_id)
        if task is None:
            return None
        return self._mark_stale(task)

    def list_tasks(self) -> list[dict[str, Any]]:
        """All dispatch tasks, newest first (summary fields only)."""
        raw: list[dict[str, Any]] = []
        for path in sorted(self._dispatch_dir.glob("*.json")):
            try:
                raw.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        self._ensure_reaper()
        raw = [self._mark_stale(t) for t in raw]
        raw.sort(key=lambda t: t.get("started_epoch") or 0.0, reverse=True)
        return [
            {
                key: task.get(key)
                for key in (
                    "task_id",
                    "task",
                    "cli",
                    "state",
                    "pid",
                    "exit_code",
                    "started_at",
                    "finished_at",
                    "updated_at",
                    "progress",
                    "approvals",
                    "escalations",
                    "terminal_summary",
                )
            }
            for task in raw
        ]

    def get_log(self, task_id: str) -> str | None:
        """Full captured stdout, or ``None`` if the task does not exist."""
        if self._read_task(task_id) is None:
            return None
        try:
            return self._log_path(task_id).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""

    # ---- lifecycle ----

    async def start(
        self,
        task_name: str,
        prompt: str,
        cwd: str | None,
        cli: str,
        timeout: float,
        wake: dict[str, Any] | None,
        origin: dict[str, Any] | None = None,
        approval_mode: str | None = None,
        evidence: str | None = None,
        max_wall_time: str | None = None,
        max_session_turns: int | None = None,
        allowed_tools: list[str] | None = None,
        resume_session_id: str | None = None,
        runs_dir: str | None = None,
        worker: str | None = None,
        runner: str | None = None,
    ) -> str:
        """Spawn a headless CLI run; returns the new task id.

        The child is ``<cli> --approval-mode <tier> --channel CI
        -o stream-json --session-id <uuid> -p <prompt>`` (args 1:1 with
        ``tools/dispatch_adapters.json``, design §4.2) with
        ``QWENPAW_SESSION_ID`` set in its env — the spawn path is
        plugin-controlled, so no middleware is involved. ``approval_mode``
        resolves flag > env > default "yolo" (#1379 parity).

        Raises:
            DispatchBusyError: same task name already running (claim flag).
            CliNotFoundError: ``cli`` binary not on PATH (hint in message).
            ValueError: ``cwd`` given but not a directory, or an invalid
                ``approval_mode``.
            SpawnFailedError: the child process could not be started.
        """
        _agent_hint = str((wake or {}).get("agent_id") or "")
        _level = _effective_approval_level(_agent_hint)
        mode = resolve_approval_mode(approval_mode, _level)
        running = self.running_task_named(task_name)
        if running is not None:
            raise DispatchBusyError(
                f"task {task_name!r} is already running "
                f"(task_id={running.get('task_id')}, "
                f"pid={running.get('pid')}); wait for it to finish or "
                "remove its task file"
            )
        bin_path = None
        if cli in ("qwen", ""):
            try:
                configured = qwen_cli()
                if configured and os.path.exists(configured):
                    bin_path = configured
            except Exception:
                bin_path = None
        bin_path = bin_path or shutil.which(cli)
        if bin_path is None:
            raise CliNotFoundError(_install_hint(cli))
        cli_version = None
        try:
            probe = await asyncio.to_thread(
                subprocess.run,
                [bin_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            cli_version = runs.parse_cli_version(probe.stdout or probe.stderr)
        except Exception:
            cli_version = None
        tested_cli_versions = {"0.26.0"}
        cli_version_supported = (
            cli_version in tested_cli_versions if cli_version else None
        )
        if cwd:
            cwd = str(Path(cwd).expanduser())
            if not os.path.isdir(cwd):
                raise ValueError(f"cwd is not a directory: {cwd}")
        else:
            cwd = None

        task_id = uuid.uuid4().hex
        session_id = (
            str(resume_session_id).strip()
            if resume_session_id and str(resume_session_id).strip()
            else str(uuid.uuid4())
        )
        resumed_from = str(resume_session_id).strip() if resume_session_id else None
        task: dict[str, Any] = {
            "task_id": task_id,
            "task": task_name,
            "cli": cli,
            "cli_path": bin_path,
            "prompt": prompt,
            "cwd": cwd,
            "session_id": session_id,
            "state": "running",
            "pid": None,
            "exit_code": None,
            "timeout": timeout,
            "started_at": _utcnow(),
            "started_epoch": time.time(),
            "updated_at": _utcnow(),
            "finished_at": None,
            "wall_s": None,
            "log_tail": "",
            "approval_mode": mode,
            "approval_level": _level or None,
            "approval_mode_source": (
                "request"
                if str(approval_mode or "").strip()
                else (
                    "env"
                    if (
                        os.environ.get(APPROVAL_MODE_ENV, "").strip()
                        or os.environ.get(LEGACY_APPROVAL_MODE_ENV, "").strip()
                    )
                    else ("level-map" if _level else "default")
                )
            ),
            "origin": dict(origin) if origin else None,
            "wake": dict(wake) if wake else None,
            "wake_status": None,
            "input_format": "text",
            "approvals": {"allowed": 0, "denied": 0, "escalated": 0},
            "progress": None,
            "evidence": str(evidence).upper() if evidence else None,
            "resumed_from": resumed_from,
            "runs_dir": runs_dir or None,
            "worker": worker or None,
            "runner": runner or None,
            "cli_version": cli_version,
            "cli_version_supported": cli_version_supported,
            "max_wall_time": max_wall_time,
            "max_session_turns": max_session_turns,
            "allowed_tools": list(allowed_tools or []),
        }
        self._write_task(task)

        env = dict(os.environ)
        env["QWENPAW_SESSION_ID"] = session_id
        try:
            _home = cli_home()
            if _home:
                env["HOME"] = _home
                _prepared = prepare_cli_home(_home)
                if _prepared:
                    env["QWEN_HOME"] = _prepared
            _gitconfig = os.path.expanduser("~/.gitconfig")
            if os.path.exists(_gitconfig):
                env.setdefault("GIT_CONFIG_GLOBAL", _gitconfig)
        except Exception:
            pass
        spawn_args, prompt_via_stdin = build_spawn_plan(
            mode,
            session_id,
            prompt,
            max_wall_time=max_wall_time,
            max_session_turns=max_session_turns,
            allowed_tools=allowed_tools,
            resume=bool(resumed_from),
        )
        self._update_task(
            task_id,
            {"input_format": "stream-json" if prompt_via_stdin else "text"},
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                bin_path,
                *spawn_args,
                cwd=cwd,
                env=env,
                stdin=(
                    asyncio.subprocess.PIPE
                    if prompt_via_stdin
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            self._update_task(
                task_id,
                {
                    "state": "failed",
                    "finished_at": _utcnow(),
                    "updated_at": _utcnow(),
                    "log_tail": f"spawn failed: {exc}",
                },
            )
            raise SpawnFailedError(
                f"failed to spawn {cli!r}: {exc}"
            ) from exc

        self._update_task(task_id, {"pid": proc.pid})
        self._ensure_reaper()
        if prompt_via_stdin and proc.stdin is not None:
            try:
                proc.stdin.write(initialize_message().encode("utf-8"))
                proc.stdin.write(user_message(session_id, prompt).encode("utf-8"))
                await proc.stdin.drain()
            except Exception:  # the pump reports the run failure
                pass
        asyncio.get_running_loop().create_task(
            self._watch(task_id, proc, timeout),
            name=f"coding-cli-dispatch-{task_id}",
        )
        # 侧栏同步（P1）：在 console 左栏 "QwenCode" 分组里建一条会话记录；失败不影响派单
        _sync = chat_sync_enabled()
        if _sync:
          try:
              _wake = task.get("wake") or {}
              _origin = task.get("origin") or {}
              _root = chats.resolve_root(_wake.get("base_url"))
              _agent = chats.resolve_agent(_wake.get("agent_id"))
              _gid = await asyncio.to_thread(chats.ensure_group, _root, _agent)
              _cid = await asyncio.to_thread(
                  chats.create_run_chat,
                  _root,
                  _agent,
                  name=chats.run_chat_name(task_name, cli, "running"),
                  session_id=f"qwen-code-bridge:{task_id}",
                  user_id=str(_origin.get("user_id") or "default"),
                  group_id=_gid,
                  meta={"task_id": task_id, "cli": cli, "task": task_name},
              )
              self._update_task(
                  task_id,
                  {"chat_id": _cid, "chat_sync": "created" if _cid else "empty"},
              )
          except Exception as exc:  # best-effort
              self._update_task(
                  task_id,
                  {"chat_sync": f"failed: {str(exc)[:120]}"},
              )
        return task_id

    @staticmethod
    def _wake_text(
        task: dict[str, Any], status: str, code: Any, wall: float
    ) -> str:
        """3-line terminal summary (dispatch_watch.py inbox semantics)."""
        summary = task.get("terminal_summary") or []
        head = (
            str(summary[0])
            if summary
            else f"[dispatch] {task.get('task')} 终态={status} "
            f"exit={code} wall={wall}s"
        )
        tail_line = (
            str(summary[2])
            if len(summary) > 2
            else "下一步：按任务书验收（复跑门/抽查 diff/给推送命令）"
        )
        return "\n".join(
            [
                head,
                f"日志: GET /api/qwen-code-bridge/dispatch/"
                f"{task.get('task_id')}/log",
                tail_line,
            ]
        )

    async def _watch(
        self,
        task_id: str,
        proc: asyncio.subprocess.Process,
        timeout: float,
    ) -> None:
        """Watchdog: poll the child (default 20s), enforce the timeout,
        write the terminal state, then send the wake (best-effort).
        """
        started = time.time()
        tail: list[str] = [""]
        killed = False
        meta = self._read_task(task_id) or {}
        stdin_mode = str(meta.get("input_format") or "") == "stream-json"
        roots = [str(meta.get("cwd") or os.getcwd())]
        tracker = progress.ProgressTracker(roots=roots)
        progress_path = self._dispatch_dir / f"{task_id}.progress.json"
        receipts_path = self._dispatch_dir / f"{task_id}.receipts.jsonl"
        approvals = {"allowed": 0, "denied": 0, "escalated": 0}
        escalations: list[dict[str, Any]] = []
        flags = {"stdin_closed": False}
        last_write = [0.0]

        async def _write_stdin(payload: str) -> None:
            if not stdin_mode or flags["stdin_closed"] or proc.stdin is None:
                return
            try:
                proc.stdin.write(payload.encode("utf-8"))
                await proc.stdin.drain()
            except Exception:
                pass

        async def _close_stdin() -> None:
            if (
                stdin_mode
                and not flags["stdin_closed"]
                and proc.stdin is not None
            ):
                flags["stdin_closed"] = True
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        def _persist_progress(state: str) -> None:
            snap = tracker.snapshot(state)
            try:
                progress.write_progress(progress_path, snap)
            except Exception:
                pass
            self._update_task(
                task_id,
                {"progress": snap, "updated_at": _utcnow()},
            )

        async def _handle_event(event: dict[str, Any]) -> None:
            kind = str(event.get("type") or "")
            if kind == "control_request":
                request = event.get("request") or {}
                request_id = str(event.get("request_id") or "")
                if str(request.get("subtype") or "") == "can_use_tool":
                    decision = approval.decide(
                        str(request.get("tool_name") or ""),
                        request.get("input"),
                        roots=roots,
                    )
                    await _write_stdin(
                        json.dumps(
                            approval.control_response(request_id, decision),
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    if decision.get("behavior") == "allow":
                        approvals["allowed"] += 1
                    else:
                        approvals["denied"] += 1
                    if decision.get("requires_human"):
                        approvals["escalated"] += 1
                        escalations.append(
                            {
                                "tool": str(request.get("tool_name") or ""),
                                "rule": decision.get("rule"),
                                "reason": decision.get("reason"),
                                "request_id": request_id,
                            }
                        )
                    try:
                        approval.append_receipt(
                            receipts_path,
                            approval.receipt_entry(
                                task_id,
                                str(request.get("tool_name") or ""),
                                request.get("input"),
                                decision,
                                request_id,
                            ),
                        )
                    except Exception:
                        pass
                    self._update_task(
                        task_id,
                        {
                            "approvals": dict(approvals),
                            "escalations": list(escalations),
                        },
                    )
                else:
                    await _write_stdin(
                        json.dumps(
                            {
                                "type": "control_response",
                                "response": {
                                    "subtype": "error",
                                    "request_id": request_id,
                                    "error": {
                                        "message": "unsupported control request"
                                    },
                                },
                            }
                        )
                        + "\n"
                    )
                return
            if kind in ("control_cancel_request", "control_response"):
                return
            tracker.feed(event)
            now = time.time()
            if now - last_write[0] >= 1.0:
                last_write[0] = now
                _persist_progress("running")
            if kind == "result":
                await _close_stdin()

        async def _pump() -> None:
            assert proc.stdout is not None
            with self._log_path(task_id).open("ab") as fh:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    fh.write(line)
                    fh.flush()
                    text = line.decode("utf-8", "replace")
                    tail[0] = (tail[0] + text)[-_LOG_TAIL_LIMIT:]
                    self._update_task(
                        task_id,
                        {"log_tail": tail[0], "updated_at": _utcnow()},
                    )
                    stripped = text.strip()
                    if stripped.startswith("{"):
                        try:
                            event = json.loads(stripped)
                        except ValueError:
                            event = None
                        if isinstance(event, dict):
                            try:
                                await _handle_event(event)
                            except Exception:
                                pass

        pump = asyncio.ensure_future(_pump())
        timeout_extensions = 0
        stall_after = float(
            os.environ.get("QWEN_CODE_BRIDGE_STALL_TIMEOUT", "1800") or 1800
        )
        try:
            while True:
                await asyncio.sleep(self._watch_interval)
                if proc.returncode is not None:
                    break
                if time.time() - started > timeout:
                    # The wall clock is not a liveness signal. QwenCode's live
                    # indicator + the event stream decide: while the session
                    # is busy and events keep flowing, let it run to
                    # completion (no extension cap). Kill only an idle zombie
                    # or a session with no progress for the stall window.
                    busy = False
                    try:
                        busy = await qwen_status.session_busy(
                            str(meta.get("cwd") or os.getcwd()),
                            str(meta.get("session_id") or ""),
                        )
                    except Exception:
                        busy = False
                    progress_age = time.time() - tracker.last_event
                    if keep_running_after_timeout(busy, progress_age, stall_after):
                        timeout_extensions += 1
                        timeout = (time.time() - started) + stall_after
                        self._update_task(
                            task_id,
                            {
                                "timeout_extensions": timeout_extensions,
                                "timeout": timeout,
                                "stall_after": stall_after,
                            },
                        )
                        continue
                    killed = True
                    await _close_stdin()
                    proc.kill()
                    break
                # A finished turn (result seen) should not keep the process
                # alive waiting on stdin; reap it after a short grace.
                if (
                    tracker.result is not None
                    and time.time() - tracker.last_event > 10
                ):
                    await _close_stdin()
                    proc.kill()
                    break
            code = await proc.wait()
            try:
                await pump
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            await _close_stdin()
            if not pump.done():
                pump.cancel()
                try:
                    await pump
                except (asyncio.CancelledError, Exception):
                    pass

        if killed:
            status = "timeout"
        elif code == 0:
            status = "done"
        else:
            status = "failed"
        wall = round(time.time() - started, 1)
        now = _utcnow()

        task = self._read_task(task_id) or {}
        snap = tracker.snapshot(status)
        try:
            progress.write_progress(progress_path, snap)
        except Exception:
            pass
        terminal_summary = [
            (
                f"[{task.get('task')}] {status} · exit={code} · {wall}s · "
                f"{snap['turns']} turns · {snap['tool_calls']} tools · "
                f"approvals {approvals['allowed']}a/{approvals['denied']}d"
                f"/{approvals['escalated']}e"
            ),
            (
                f"tests {snap['tests']['passed']}p/{snap['tests']['failed']}f"
                f" · last={snap['last_tool']} {snap['last_target']}"
            ).strip(),
            (
                f"失败: {snap['last_error']}"
                if snap.get("last_error")
                else (
                    f"需人工: {len(escalations)} 条"
                    f"（首条 {escalations[0]['tool']}: "
                    f"{escalations[0]['reason']}；见 receipts）"
                    if escalations
                    else "下一步：按任务书验收（复跑门/抽查 diff/给推送命令）"
                )
            ),
        ]
        wake = task.get("wake") or {}
        origin = task.get("origin") or {}
        wake_session_id = str(
            origin.get("session_id") or wake.get("session_id") or ""
        ).strip()
        # Terminal write carries the wake status: "pending" until the
        # (best-effort) inject lands, "skipped" when there is no target.
        self._update_task(
            task_id,
            {
                "state": status,
                "exit_code": code,
                "wall_s": wall,
                "finished_at": now,
                "updated_at": now,
                "log_tail": tail[0],
                "wake_status": "pending" if wake_session_id else "skipped",
                "notify_tier": millforge.notify_tier(status),
                "progress": snap,
                "approvals": dict(approvals),
                "escalations": list(escalations),
                "terminal_summary": terminal_summary,
            },
        )

        # Q3: AgentTeams coding-runs index (only when runs_dir is configured)
        try:
            log_text = await asyncio.to_thread(self.get_log, task_id) or ""
            # Re-read AFTER the terminal update: the earlier snapshot still
            # says "running" (status would map to failed, exit/finished None).
            run_task = self._read_task(task_id) or task
            rec = await asyncio.to_thread(
                runs.write_run_record,
                run_task,
                log_text,
                run_task.get("runs_dir"),
            )
            if rec:
                self._update_task(
                    task_id,
                    {"run_record": rec["record"], "run_log": rec["run"]},
                )
        except Exception:
            pass

        # 侧栏同步（P1）：终态改名（状态进名字）
        _chat_id = str(task.get("chat_id") or "")
        if _chat_id:
            try:
                await asyncio.to_thread(
                    chats.rename_chat,
                    chats.resolve_root(wake.get("base_url")),
                    chats.resolve_agent(wake.get("agent_id")),
                    _chat_id,
                    chats.run_chat_name(
                        str(task.get("task") or ""),
                        str(task.get("cli") or ""),
                        status,
                    ),
                )
                self._update_task(task_id, {"chat_sync": f"renamed:{status}"})
            except Exception as exc:  # best-effort
                self._update_task(
                    task_id,
                    {"chat_sync": f"rename_failed: {str(exc)[:80]}"},
                )

        if wake_session_id:
            text = self._wake_text(task, status, code, wall)
            try:
                await asyncio.to_thread(
                    self._wake_sender,
                    wake_session_id,
                    str(wake.get("agent_id") or ""),
                    str(wake.get("base_url") or ""),
                    text,
                    str(origin.get("channel") or wake.get("channel") or "console"),
                    str(origin.get("user_id") or wake.get("user_id") or "default"),
                    bool(wake.get("room_bell")),
                )
                wake_status = "sent"
            except Exception as exc:  # wake is best-effort; state stands
                wake_status = f"failed: {str(exc)[:120]}"
            self._update_task(
                task_id, {"wake_status": wake_status, "updated_at": _utcnow()}
            )


__all__ = [
    "APPROVAL_MODES",
    "APPROVAL_MODE_ENV",
    "DEFAULT_APPROVAL_MODE",
    "DEFAULT_DISPATCH_TIMEOUT",
    "SPAWN_ARG_TEMPLATE",
    "WATCH_INTERVAL_ENV",
    "CliNotFoundError",
    "DispatchBusyError",
    "DispatchService",
    "SpawnFailedError",
    "WakeSender",
    "default_wake_sender",
    "resolve_approval_mode",
]
