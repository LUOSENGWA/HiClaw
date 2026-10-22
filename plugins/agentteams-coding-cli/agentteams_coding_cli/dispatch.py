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
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from .registry import CLIS
from .state import state_dir

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

_DEFAULT_WAKE_URL = "http://127.0.0.1:8088"
_WAKE_ENDPOINT = "/api/console/chat"
_WAKE_ATTEMPTS = 3

#: Headless spawn args, 1:1 with the production adapter.
#: Authoritative source: ``tools/dispatch_adapters.json`` (entry "qwen",
#: key "args"); ``{sid}`` / ``{prompt}`` are substituted in ``start()``.
SPAWN_ARG_TEMPLATE: tuple[str, ...] = (
    "--approval-mode",
    "yolo",
    "--channel",
    "CI",
    "-o",
    "stream-json",
    "--session-id",
    "{sid}",
    "-p",
    "{prompt}",
)


class DispatchBusyError(RuntimeError):
    """Raised when a dispatch with the same task name is already running."""


class CliNotFoundError(RuntimeError):
    """Raised when the requested CLI binary is not on PATH."""


class SpawnFailedError(RuntimeError):
    """Raised when the child process cannot be started."""


#: ``wake_sender(session_id, agent_id, base_url, text) -> None``;
#: returns on success, raises on final failure.
WakeSender = Callable[[str, str, str, str], None]


def default_wake_sender(
    session_id: str,
    agent_id: str,
    base_url: str,
    text: str,
) -> None:
    """Real-HTTP session inject (dispatch_notify.py default backend).

    POSTs the terminal summary to ``<base>/api/console/chat`` with the
    ``X-Agent-Id`` header; retries on 409/429/503 and transient errors
    (3 attempts). Raises ``RuntimeError`` on final failure.
    """
    root = (
        base_url
        or os.environ.get("QWENPAW_APP_URL", "").strip()
        or _DEFAULT_WAKE_URL
    ).rstrip("/")
    body = {
        "channel": "console",
        "user_id": "default",
        "session_id": session_id,
        "input": [{"content": [{"type": "text", "text": text}], "metadata": {}}],
    }
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
                f"POST /api/agentteams-coding-cli/{spec.id}/install"
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

    def running_task_named(self, task_name: str) -> dict[str, Any] | None:
        """Claim flag: a ``running`` task file with the same task name."""
        for path in sorted(self._dispatch_dir.glob("*.json")):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if task.get("task") == task_name and task.get("state") == "running":
                return task
        return None

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._read_task(task_id)

    def list_tasks(self) -> list[dict[str, Any]]:
        """All dispatch tasks, newest first (summary fields only)."""
        raw: list[dict[str, Any]] = []
        for path in sorted(self._dispatch_dir.glob("*.json")):
            try:
                raw.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
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
    ) -> str:
        """Spawn a headless CLI run; returns the new task id.

        The child is ``<cli> --approval-mode yolo --channel CI
        -o stream-json --session-id <uuid> -p <prompt>`` (args 1:1 with
        ``tools/dispatch_adapters.json``, design §4.2) with
        ``QWENPAW_SESSION_ID`` set in its env — the spawn path is
        plugin-controlled, so no middleware is involved.

        Raises:
            DispatchBusyError: same task name already running (claim flag).
            CliNotFoundError: ``cli`` binary not on PATH (hint in message).
            ValueError: ``cwd`` given but not a directory.
            SpawnFailedError: the child process could not be started.
        """
        running = self.running_task_named(task_name)
        if running is not None:
            raise DispatchBusyError(
                f"task {task_name!r} is already running "
                f"(task_id={running.get('task_id')}, "
                f"pid={running.get('pid')}); wait for it to finish or "
                "remove its task file"
            )
        bin_path = shutil.which(cli)
        if bin_path is None:
            raise CliNotFoundError(_install_hint(cli))
        if cwd:
            cwd = str(Path(cwd).expanduser())
            if not os.path.isdir(cwd):
                raise ValueError(f"cwd is not a directory: {cwd}")
        else:
            cwd = None

        task_id = uuid.uuid4().hex
        session_id = str(uuid.uuid4())
        task: dict[str, Any] = {
            "task_id": task_id,
            "task": task_name,
            "cli": cli,
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
            "wake": dict(wake) if wake else None,
            "wake_status": None,
        }
        self._write_task(task)

        env = dict(os.environ)
        env["QWENPAW_SESSION_ID"] = session_id
        spawn_args: list[str] = []
        for item in SPAWN_ARG_TEMPLATE:
            if item == "{prompt}":
                spawn_args.append(prompt)
            elif item == "{sid}":
                spawn_args.append(session_id)
            else:
                spawn_args.append(item)
        try:
            proc = await asyncio.create_subprocess_exec(
                bin_path,
                *spawn_args,
                cwd=cwd,
                env=env,
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
        asyncio.get_running_loop().create_task(
            self._watch(task_id, proc, timeout),
            name=f"coding-cli-dispatch-{task_id}",
        )
        return task_id

    @staticmethod
    def _wake_text(
        task: dict[str, Any], status: str, code: Any, wall: float
    ) -> str:
        """3-line terminal summary (dispatch_watch.py inbox semantics)."""
        return "\n".join(
            [
                f"[dispatch] {task.get('task')} 终态={status} "
                f"exit={code} wall={wall}s",
                f"日志: GET /api/agentteams-coding-cli/dispatch/"
                f"{task.get('task_id')}/log",
                "下一步：按任务书验收（复跑门/抽查 diff/给推送命令）",
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

        async def _pump() -> None:
            assert proc.stdout is not None
            with self._log_path(task_id).open("ab") as fh:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    fh.write(line)
                    fh.flush()
                    tail[0] = (
                        tail[0] + line.decode("utf-8", "replace")
                    )[-_LOG_TAIL_LIMIT:]
                    self._update_task(
                        task_id,
                        {"log_tail": tail[0], "updated_at": _utcnow()},
                    )

        pump = asyncio.ensure_future(_pump())
        try:
            while True:
                await asyncio.sleep(self._watch_interval)
                if proc.returncode is not None:
                    break
                if time.time() - started > timeout:
                    killed = True
                    proc.kill()
                    break
            code = await proc.wait()
            await pump
        finally:
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
        wake = task.get("wake") or {}
        wake_session_id = str(wake.get("session_id") or "").strip()
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
            },
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
                )
                wake_status = "sent"
            except Exception as exc:  # wake is best-effort; state stands
                wake_status = f"failed: {str(exc)[:120]}"
            self._update_task(
                task_id, {"wake_status": wake_status, "updated_at": _utcnow()}
            )


__all__ = [
    "DEFAULT_DISPATCH_TIMEOUT",
    "SPAWN_ARG_TEMPLATE",
    "WATCH_INTERVAL_ENV",
    "CliNotFoundError",
    "DispatchBusyError",
    "DispatchService",
    "SpawnFailedError",
    "WakeSender",
    "default_wake_sender",
]
