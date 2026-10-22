# -*- coding: utf-8 -*-
"""API endpoints for managing coding CLIs (worker container).

Ported from QwenPaw upstream PR 8156
(``src/qwenpaw/app/routers/coding_cli.py``). Deltas vs upstream:

- No ``app.state`` service: the service is constructed per request via
  FastAPI ``Depends`` (all state is file-backed, see ``service.py`` /
  ``state.py``), so the router carries no module-level mutable state.
- The URL prefix is applied by ``plugin.py`` through
  ``PluginApi.register_http_router(prefix="/agentteams-coding-cli")``;
  this router therefore defines no prefix of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .registry import CLIS, get_cli
from .service import CodingCliService, InstallBusyError

router = APIRouter(tags=["agentteams-coding-cli"])


def _get_service() -> CodingCliService:
    """Per-request service; all state is file-backed under the state dir."""
    return CodingCliService()


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
