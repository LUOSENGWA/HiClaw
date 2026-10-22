# -*- coding: utf-8 -*-
"""API endpoints for managing coding CLIs (worker container).

Management surface ported from QwenPaw upstream PR 8156
(``src/qwenpaw/app/routers/coding_cli.py``); the P2 dispatch surface
(design §4.2, gateless per the 10/22 ruling) is defined first so its
``/dispatch*`` routes take precedence over the ``/{cli_id}/...``
patterns. Deltas vs upstream:

- No ``app.state`` service: the service is constructed per request via
  FastAPI ``Depends`` (all state is file-backed, see ``service.py`` /
  ``state.py``), so the router carries no module-level mutable state.
- The URL prefix is applied by ``plugin.py`` through
  ``PluginApi.register_http_router(prefix="/qwen-code-bridge")``;
  this router therefore defines no prefix of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .dispatch import (
    DEFAULT_DISPATCH_TIMEOUT,
    CliNotFoundError,
    DispatchBusyError,
    DispatchService,
    SpawnFailedError,
)
from .registry import CLIS, get_cli
from .service import CodingCliService, InstallBusyError

router = APIRouter(tags=["qwen-code-bridge"])


def _get_service() -> CodingCliService:
    """Per-request service; all state is file-backed under the state dir."""
    return CodingCliService()


def _get_dispatch_service() -> DispatchService:
    """Per-request dispatch service; all state is file-backed."""
    return DispatchService()


def _require_cli(cli_id: str):
    spec = get_cli(cli_id)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown coding CLI: {cli_id}",
        )
    return spec


class SettingsPatch(BaseModel):
    """Partial settings to merge into the CLI settings file."""

    settings: dict[str, Any] = Field(default_factory=dict)


class InstallRequest(BaseModel):
    """Install/upgrade request.

    ``tag`` accepts an npm dist-tag (``latest``, ``nightly``) or a
    concrete version string (``0.25.0``).
    """

    tag: str = "latest"


class WakeSpec(BaseModel):
    """Terminal-state wake target (session inject, dispatch_notify semantics).

    ``session_id`` selects the QwenPaw session to wake; empty/absent
    means no wake. ``base_url`` overrides the app URL (else the
    ``QWENPAW_APP_URL`` env var, else ``http://127.0.0.1:8088``).

    ``channel`` selects the delivery surface. ``console`` = same-app
    session inject (default). AgentTeams deployments run the **plugin**
    channel ``agentteams_matrix`` (NOT the built-in ``matrix`` key);
    ``session_id`` must be ``matrix:<room_id>``.

    ``user_id`` is part of the session identity: QwenPaw keys session
    state on ``session_id`` **and** ``user_id``. For team group
    rooms ``share_session_in_group=true`` maps ``user_id`` to the
    **room_id** (legacy shared session); ``false`` maps it to the real
    sender (per-sender isolation). DMs use the room_id.
    ``room_bell`` (matrix only) additionally posts the 3-line terminal
    summary to the room via ``POST /api/messages/send``.
    """

    session_id: str = ""
    agent_id: str = ""
    base_url: str | None = None
    channel: str = "console"
    user_id: str = "default"
    room_bell: bool = False


class OriginSpec(BaseModel):
    """Source session the task was dispatched from (channel-agnostic).

    QwenPaw identifies a session by ``(session_id, user_id, channel)``
    (``ChatManager.get_or_create_chat``). Recording the caller's own
    triple lets the terminal wake replay it verbatim — no channel
    inference, no cross-channel guessing.
    """

    session_id: str = ""
    user_id: str = "default"
    channel: str = "console"


class DispatchRequest(BaseModel):
    """Gateless dispatch (10/22 ruling: no parallel/conflict gates).

    ``cli`` is the executable name on PATH (default ``qwen``);
    ``cwd`` is the working directory (e.g. a worktree) for the run.
    ``files_to_touch`` is pure metadata accepted for interface
    parity with the old dispatcher: it is never persisted and never
    consumed (no gate reads it).
    """

    task: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    cwd: str | None = None
    cli: str = "qwen"
    timeout: float = Field(default=DEFAULT_DISPATCH_TIMEOUT, ge=1)
    files_to_touch: list[str] = Field(default_factory=list)
    evidence: str | None = Field(
        default=None,
        description="Claimed evidence grade: A (merged/deployed+E2E), B (code+tests), C (design), D (retired).",
    )
    max_wall_time: str | None = Field(
        default=None,
        description="#1379 native wall budget (qwen format, e.g. 90 / 5m / 1h).",
    )
    max_session_turns: int | None = Field(default=None, ge=0)
    allowed_tools: list[str] = Field(default_factory=list)
    resume_session_id: str | None = Field(
        default=None,
        description="Existing CLI session id; re-dispatch with --resume so a kill+re-dispatch keeps context (steering).",
    )
    approval_mode: str | None = Field(
        default=None,
        description=(
            "Per-run qwen approval tier (plan/default/auto-edit/auto/yolo); "
            "resolution: request > env QWEN_CODE_BRIDGE_APPROVAL_MODE > "
            "'yolo' (#1379 parity)."
        ),
    )
    origin: OriginSpec | None = None
    wake: WakeSpec | None = None


# ---- dispatch surface (design §4.2) ----
# Defined before the /{cli_id}/... routes so /dispatch* wins over
# the generic segment patterns.


@router.post("/dispatch", status_code=202)
async def post_dispatch(
    body: DispatchRequest,
    request: Request,
    service: DispatchService = Depends(_get_dispatch_service),
) -> dict:
    """Spawn a gateless headless CLI run; returns the task id.

    409: same task name already running (claim flag), or the CLI is
    not on PATH (detail carries an install hint — no auto-install).
    400: ``cwd`` given but not a directory.
    """
    wake = body.wake.model_dump() if body.wake is not None else None
    origin = body.origin.model_dump() if body.origin is not None else None
    if wake is not None and not str(wake.get("base_url") or "").strip():
        wake["base_url"] = str(request.base_url).rstrip("/")
    try:
        task_id = await service.start(
            task_name=body.task,
            prompt=body.prompt,
            cwd=body.cwd,
            cli=body.cli,
            timeout=body.timeout,
            wake=wake,
            origin=origin,
            approval_mode=body.approval_mode,
            evidence=body.evidence,
            max_wall_time=body.max_wall_time,
            max_session_turns=body.max_session_turns,
            allowed_tools=body.allowed_tools,
            resume_session_id=body.resume_session_id,
        )
    except DispatchBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CliNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SpawnFailedError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"task_id": task_id}


class SidebarRequest(BaseModel):
    """Optional agent override for sidebar provisioning."""

    agent_id: str = ""


@router.post("/sidebar")
async def post_sidebar(
    request: Request,
    body: SidebarRequest | None = None,
) -> dict:
    """Ensure the pinned QwenCode group + the direct-chat session exist."""
    import asyncio

    from . import chats
    from .endpoints import app_base

    agent = chats.resolve_agent(
        (body.agent_id if body else "")
        or None,
    )
    root = app_base(None, request)
    group_id = await asyncio.to_thread(chats.ensure_group, root, agent)
    chat_id = await asyncio.to_thread(
        chats.ensure_direct_chat,
        root,
        agent,
        group_id,
    )
    return {
        "agent_id": agent,
        "group_id": group_id,
        "direct_chat_id": chat_id,
        "direct_session_id": chats.DIRECT_SESSION_ID,
    }



@router.get("/chat/{chat_id}")
async def get_chat_meta(chat_id: str, request: Request) -> dict:
    """Channel lookup for the console chat wrapper (server-side, no auth)."""
    from qwenpaw.app.agent_context import get_agent_for_request

    try:
        workspace = await get_agent_for_request(request)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    manager = getattr(workspace, "chat_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="chat manager unavailable")
    chat = await manager.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return {
        "chat_id": chat.id,
        "name": chat.name,
        "channel": chat.channel,
        "session_id": chat.session_id,
        "group_id": chat.group_id,
    }


@router.get("/dispatch")
async def list_dispatch(
    format: str = "",
    service: DispatchService = Depends(_get_dispatch_service),
):
    """List dispatch tasks (newest first).

    ``?format=table`` renders the one-table human view: state, phase,
    elapsed, last tool, approvals and the terminal summary in three
    columns per task (design §1.1 goal 6).
    """
    tasks = service.list_tasks()
    if str(format or "").strip().lower() != "table":
        return {"tasks": tasks}
    lines = ["TASK | STATE | PHASE | ELAPSED | LAST | APPROVALS"]
    for task in tasks:
        prog = task.get("progress") or {}
        el = prog.get("elapsed_s")
        lines.append(
            " | ".join(
                [
                    str(task.get("task") or ""),
                    str(task.get("state") or ""),
                    str(prog.get("phase") or "-"),
                    f"{el}s" if isinstance(el, (int, float)) else "-",
                    str(prog.get("last_tool") or "-"),
                    _approval_cell(task.get("approvals")),
                ]
            )
        )
    return PlainTextResponse("\n".join(lines) + "\n")


def _approval_cell(value) -> str:
    if not isinstance(value, dict):
        return "-"
    return (
        f"{int(value.get('allowed') or 0)}a/"
        f"{int(value.get('denied') or 0)}d/"
        f"{int(value.get('escalated') or 0)}e"
    )


@router.get("/dispatch/{task_id}")
async def get_dispatch(
    task_id: str,
    service: DispatchService = Depends(_get_dispatch_service),
) -> dict:
    """Dispatch task state (running/done/failed/timeout + exit + log tail)."""
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/dispatch/{task_id}/progress")
async def get_dispatch_progress(
    task_id: str,
    service: DispatchService = Depends(_get_dispatch_service),
) -> dict:
    """Derived live progress (phase/last tool/turns/tests/stalled)."""
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task_id,
        "state": task.get("state"),
        "progress": task.get("progress"),
        "approvals": task.get("approvals"),
        "terminal_summary": task.get("terminal_summary"),
    }


@router.get("/dispatch/{task_id}/run")
async def get_dispatch_run(
    task_id: str,
    service: DispatchService = Depends(_get_dispatch_service),
) -> dict:
    """T5: project the task onto the coding-runs read contract (千锻)."""
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    from . import millforge

    return millforge.coding_run_record(task)


@router.post("/dispatch/cleanup")
async def post_dispatch_cleanup(
    kill: bool = False,
    service: DispatchService = Depends(_get_dispatch_service),
) -> dict:
    """Mark dead ``running`` tasks as stale (never kills live runs)."""
    return service.cleanup(kill=kill)


@router.get("/dispatch/{task_id}/log")
async def get_dispatch_log(
    task_id: str,
    service: DispatchService = Depends(_get_dispatch_service),
) -> dict:
    """Full captured stdout of a dispatch task."""
    if service.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task_id,
        "log": service.get_log(task_id) or "",
    }


@router.get("")
async def list_coding_clis(
    service: CodingCliService = Depends(_get_service),
) -> dict:
    """Probe and summarize all registered coding CLIs."""
    clis = [await service.probe(spec) for spec in CLIS.values()]
    return {"clis": clis}


@router.get("/{cli_id}/settings")
async def get_coding_cli_settings(
    cli_id: str,
    service: CodingCliService = Depends(_get_service),
) -> dict:
    """Read CLI settings with credentials redacted."""
    spec = _require_cli(cli_id)
    settings = await service.read_settings(spec)
    return {"cli": spec.id, "settings": settings}


@router.put("/{cli_id}/settings")
async def put_coding_cli_settings(
    cli_id: str,
    body: SettingsPatch,
    service: CodingCliService = Depends(_get_service),
) -> dict:
    """Merge settings; ``"***"`` values keep the existing secret."""
    spec = _require_cli(cli_id)
    try:
        settings = await service.write_settings(spec, body.settings)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to write settings: {exc}",
        ) from exc
    return {"cli": spec.id, "settings": settings}


@router.post("/{cli_id}/install", status_code=202)
async def post_coding_cli_install(
    cli_id: str,
    body: InstallRequest,
    service: CodingCliService = Depends(_get_service),
) -> dict:
    """Start an async npm install/upgrade for the CLI."""
    spec = _require_cli(cli_id)
    try:
        task_id = await service.start_install(spec, body.tag)
    except InstallBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task_id": task_id, "cli": spec.id, "tag": body.tag}


@router.get("/{cli_id}/install/{task_id}")
async def get_coding_cli_install(
    cli_id: str,
    task_id: str,
    service: CodingCliService = Depends(_get_service),
) -> dict:
    """Report install task state."""
    spec = _require_cli(cli_id)
    task = service.install_status(task_id)
    if task is None or task.get("cli") != spec.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def build_router() -> APIRouter:
    """Router factory consumed by ``plugin.py``."""
    from .web import router as web_router

    if not any(
        getattr(route, "path", "") == "/web/status"
        for route in router.routes
    ):
        # /web/* must match before the parameterized management routes
        # (/{cli_id}/settings would otherwise swallow /web/settings).
        router.routes = list(web_router.routes) + list(router.routes)
    return router


__all__ = ["router", "build_router"]
