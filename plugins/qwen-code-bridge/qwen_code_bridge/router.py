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

from fastapi import APIRouter, Depends, HTTPException
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
    service: DispatchService = Depends(_get_dispatch_service),
) -> dict:
    """Spawn a gateless headless CLI run; returns the task id.

    409: same task name already running (claim flag), or the CLI is
    not on PATH (detail carries an install hint — no auto-install).
    400: ``cwd`` given but not a directory.
    """
    wake = body.wake.model_dump() if body.wake is not None else None
    origin = body.origin.model_dump() if body.origin is not None else None
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


@router.get("/dispatch")
async def list_dispatch(
    service: DispatchService = Depends(_get_dispatch_service),
) -> dict:
    """List dispatch tasks (newest first)."""
    return {"tasks": service.list_tasks()}


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
    return router


__all__ = ["router", "build_router"]
