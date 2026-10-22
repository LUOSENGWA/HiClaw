# -*- coding: utf-8 -*-
"""P3 — expose the Qwen Code web surface through QwenPaw.

``qwen serve`` (HTTP + Web UI + token) runs locally; this module gives the
plugin a small reverse proxy plus a reachability probe, so the Qwen Code
web features (sessions, streaming, tool calls) can be opened from the
QwenPaw side without embedding a custom frontend.

Env:
  QWEN_CODE_BRIDGE_SERVE_URL    base URL (default ``http://127.0.0.1:8931``)
  QWEN_CODE_BRIDGE_SERVE_TOKEN  bearer token forwarded to ``qwen serve``

All failures are reported as data, never raised into the caller's turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

SERVE_URL_ENV = "QWEN_CODE_BRIDGE_SERVE_URL"
SERVE_TOKEN_ENV = "QWEN_CODE_BRIDGE_SERVE_TOKEN"
DEFAULT_SERVE_URL = "http://127.0.0.1:8931"

router = APIRouter(tags=["qwen-code-bridge"])


def _config() -> dict:
    """Optional ``<state_dir>/serve.json`` = {url, token} (env wins)."""
    try:
        from .state import state_dir

        path: Path = state_dir() / "serve.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def serve_url() -> str:
    return (
        os.environ.get(SERVE_URL_ENV, "").strip()
        or str(_config().get("url") or "").strip()
        or DEFAULT_SERVE_URL
    ).rstrip("/")


def serve_token() -> str:
    return (
        os.environ.get(SERVE_TOKEN_ENV, "").strip()
        or str(_config().get("token") or "").strip()
    )


def _headers(request: Request | None = None) -> dict[str, str]:
    token = serve_token()
    headers = {"Accept": "application/json, text/plain, */*"}
    if request is not None:
        content_type = request.headers.get("content-type")
        if content_type:
            headers["Content-Type"] = content_type
        accept = request.headers.get("accept")
        if accept:
            headers["Accept"] = accept
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@router.get("/web/status")
async def web_status() -> dict:
    """Probe ``qwen serve`` reachability (never raises)."""
    url = serve_url()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url + "/health", headers=_headers())
        return {
            "reachable": resp.status_code < 500,
            "url": url,
            "status_code": resp.status_code,
        }
    except Exception as exc:  # unreachable / timeout
        return {
            "reachable": False,
            "url": url,
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


@router.api_route(
    "/web/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def web_proxy(path: str, request: Request) -> Response:
    """Reverse-proxy a request to the local ``qwen serve`` instance."""
    url = f"{serve_url()}/{path}"
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            upstream = await client.request(
                request.method,
                url,
                content=body or None,
                headers=_headers(request),
                params=dict(request.query_params),
            )
    except Exception as exc:  # report as data, don't break the HTTP layer
        return JSONResponse(
            status_code=502,
            content={
                "error": "serve-unreachable",
                "url": url,
                "detail": f"{type(exc).__name__}: {str(exc)[:160]}",
            },
        )
    media_type = upstream.headers.get("content-type", "application/octet-stream")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=media_type,
    )


__all__ = ["router", "serve_url"]
