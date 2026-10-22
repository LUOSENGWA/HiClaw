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

import asyncio
import json
import mimetypes
import os
import re
import secrets
import socket
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

SERVE_URL_ENV = "QWEN_CODE_BRIDGE_SERVE_URL"
SERVE_AUTOSTART_ENV = "QWEN_CODE_BRIDGE_SERVE_AUTOSTART"
SERVE_WORKSPACE_ENV = "QWEN_CODE_BRIDGE_SERVE_WORKSPACE"
WEB_DIST_ENV = "QWEN_CODE_BRIDGE_WEB_DIST"
SERVE_TOKEN_ENV = "QWEN_CODE_BRIDGE_SERVE_TOKEN"
DEFAULT_SERVE_URL = ""  # resolved from env/serve.json; auto-start allocates a free port

#: Where this proxy is mounted; the embedded console treats it as its root.
WEB_PREFIX = "/api/qwen-code-bridge/web"

#: Injected into the console HTML: prefix every root-absolute request the
#: shell makes (fetch / XHR / WebSocket / EventSource) so a prefixed mount
#: behaves like the original origin root.
_PREFIX_SHIM = """<script>(function(){
var P=location.pathname.replace(/\\/?[^/]*\\/?$/,"")||"";
function fix(u){try{
  if(typeof u!=="string") return u;
  if(u.indexOf(P+"/")===0) return u;
  if(u.charAt(0)==="/") return P+u;
  var o=location.origin;
  if(o && u.indexOf(o+"/")===0) return o+P+u.slice(o.length);
}catch(e){}return u;}
var f=window.fetch;if(f){window.fetch=function(u,o){
  try{if(typeof u==="object"&&u&&typeof u.url==="string"&&window.Request&&u instanceof Request){
    var nu=fix(u.url); if(nu!==u.url){u=new Request(nu,u);}
  }}catch(e){}
  return f.call(this,fix(u),o)};}
  try{if(typeof u==="object"&&u&&typeof u.url==="string"&&window.Request&&u instanceof Request){
    var nu=fix(u.url); if(nu!==u.url){u=new Request(nu,u);}
  }}catch(e){}
var x=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){var a=[].slice.call(arguments);a[1]=fix(u);return x.apply(this,a)};
var W=window.WebSocket;if(W){window.WebSocket=function(u,p){return new W(fix(u),p)};window.WebSocket.prototype=W.prototype;}
var E=window.EventSource;if(E){window.EventSource=function(u,o){return new E(fix(u),o)};window.EventSource.prototype=E.prototype;}
var WK=window.Worker;
if(WK){
  var bootCommon="var P="+JSON.stringify(P)+";function fix(u){try{if(typeof u!==\"string\")return u;if(u.indexOf(P+\"/\")===0)return u;if(u.charAt(0)===\"/\")return P+u;var o=location.origin;if(o&&u.indexOf(o+\"/\")===0)return o+P+u.slice(o.length);}catch(e){}return u;}var _f=self.fetch;self.fetch=function(u,o){return _f(fix(u),o)};if(self.XMLHttpRequest){var _x=self.XMLHttpRequest.prototype.open;self.XMLHttpRequest.prototype.open=function(m,u){var a=[].slice.call(arguments);a[1]=fix(u);return _x.apply(this,a)}};";
  window.Worker=function(u,o){try{
    if(String(u).indexOf("blob:")===0&&o&&o.type==="module"){
      var b2=bootCommon+"await import("+JSON.stringify(String(u))+");";
      var w2=URL.createObjectURL(new Blob([b2],{type:"text/javascript"}));
      return new WK(w2,{type:"module"});
    }
    if(String(u).indexOf("blob:")===0){
      var b1=bootCommon+"importScripts("+JSON.stringify(String(u))+");";
      var w1=URL.createObjectURL(new Blob([b1],{type:"text/javascript"}));
      return new WK(w1,o);
    }
  }catch(e){}return new WK(u,o)};
}
})();</script>"""


def _rewrite_console_html(html: bytes) -> bytes:
    """Prefix root-absolute shell assets and inject the request shim."""
    text = html.decode("utf-8", "ignore")
    for path in ("/assets/", "/manifest.webmanifest", "/favicon"):
        text = text.replace('="' + path, '="' + WEB_PREFIX + path)
    if _PREFIX_SHIM not in text:
        text = text.replace("<head>", "<head>" + _PREFIX_SHIM, 1)
    return text.encode("utf-8")

router = APIRouter(tags=["qwen-code-bridge"])


_WS_SUFFIXES = ("sessions/live-state", "session-groups", "sessions", "session", "git")


def _fix_workspace_path(path: str) -> str:
    """Encode an absolute workspace selector (``/workspaces//tmp/...``).

    The shell emits ``/workspaces/<cwd>/<tail>`` unencoded; the daemon expects
    one path segment, so ``/tmp`` must become ``%2Ftmp``.
    """
    match = re.match(r"^(/?workspaces?)/(.*)$", path)
    if not match:
        return path
    base, rest = match.group(1), match.group(2)
    if not rest.startswith("/"):
        return path
    for suffix in _WS_SUFFIXES:
        idx = rest.rfind("/" + suffix)
        if idx > 0:
            from urllib.parse import quote

            workspace = rest[:idx]
            tail = rest[idx + 1:]
            return f"{base}/{quote(workspace, safe='')}/{tail}"
    return path


def web_dist() -> Path | None:
    """Optional rebuilt web-shell (base-prefixed). Served statically when present."""
    try:
        from .state import state_dir

        raw = os.environ.get(WEB_DIST_ENV, "").strip()
        path = Path(raw) if raw else state_dir() / "web-shell" / "dist"
        return path if (path / "index.html").is_file() else None
    except Exception:
        return None


def _inject_daemon_base(data: bytes) -> bytes:
    html = data.decode("utf-8", "ignore")
    inject = (
        "<script>window.__QWEN_DAEMON_BASE__="
        + json.dumps(WEB_PREFIX)
        + ";</script>"
    )
    if "__QWEN_DAEMON_BASE__" not in html:
        html = html.replace("<head>", "<head>" + inject, 1)
    return html.encode("utf-8")


def _spa_index(dist: Path) -> bytes:
    return _inject_daemon_base((dist / "index.html").read_bytes())


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
    explicit = (
        os.environ.get(SERVE_URL_ENV, "").strip()
        or str(_config().get("url") or "").strip()
    )
    if explicit:
        return explicit.rstrip("/")
    return ""


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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_config(url: str, token: str) -> None:
    try:
        from .state import state_dir

        path = state_dir() / "serve.json"
        path.write_text(
            json.dumps({"url": url, "token": token}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


async def _probe(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url + "/health", headers=_headers())
        return resp.status_code < 500
    except Exception:
        return False


async def ensure_serve() -> str:
    """Return a reachable serve URL, starting one on a free port if needed."""
    url = serve_url()
    if url and await _probe(url):
        return url
    if os.environ.get(SERVE_AUTOSTART_ENV, "1") == "0":
        return url
    port = _free_port()
    token = serve_token() or secrets.token_urlsafe(16)
    url = f"http://127.0.0.1:{port}"
    binary = os.environ.get("QWEN_CODE_BRIDGE_CLI", "").strip() or "qwen"
    try:
        from .state import state_dir

        workspaces: list[str] = []
        env_ws = os.environ.get(SERVE_WORKSPACE_ENV, "").strip()
        for chunk in env_ws.replace(":", ",").split(","):
            chunk = chunk.strip()
            if chunk and os.path.isdir(chunk) and chunk not in workspaces:
                workspaces.append(chunk)
        try:  # every run's cwd becomes a serve workspace, in recency order
            import json as _json
            import time as _time

            tasks = []
            for path in (state_dir() / "dispatch").glob("*.json"):
                try:
                    task = _json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                cwd = str(task.get("cwd") or "").strip()
                if cwd and os.path.isdir(cwd):
                    tasks.append((float(task.get("started_epoch") or 0), cwd))
            for _, cwd in sorted(tasks, reverse=True):
                if cwd not in workspaces:
                    workspaces.append(cwd)
        except Exception:
            pass
        # primary workspace first: the first entry drives the console's default
        # project view, so task cwds must precede the plugin's state dir.
        state = str(state_dir())
        if state in workspaces:
            workspaces.remove(state)
        workspaces.append(state)
        ws_args: list[str] = []
        for workspace in workspaces[:8]:
            ws_args += ["--workspace", workspace]
        proc = await asyncio.create_subprocess_exec(
            binary,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
            "--token",
            token,
            *ws_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        return url
    for _ in range(20):
        await asyncio.sleep(0.5)
        if await _probe(url):
            _write_config(url, token)
            os.environ[SERVE_URL_ENV] = url
            os.environ[SERVE_TOKEN_ENV] = token
            return url
        if proc.returncode is not None:
            break
    _write_config(url, token)
    return url


@router.get("/web/status")
async def web_status() -> dict:
    """Probe ``qwen serve`` reachability (never raises)."""
    url = await ensure_serve()
    if not url:
        return {"reachable": False, "url": "", "error": "serve-url-unconfigured"}
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
    dist = web_dist()
    if dist is not None and request.method == "GET":
        rel = (path or "").strip("/")
        candidate = (dist / rel) if rel else (dist / "index.html")
        try:
            candidate.resolve().relative_to(dist.resolve())
        except Exception:
            candidate = None
        if candidate is not None and candidate.is_file():
            ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            data = candidate.read_bytes()
            if ctype.startswith("text/html"):
                data = _inject_daemon_base(data)
            return Response(content=data, status_code=200, media_type=ctype)
    base = await ensure_serve()
    if not base:
        return JSONResponse(
            status_code=502,
            content={"error": "serve-url-unconfigured"},
        )
    path = _fix_workspace_path(path)
    url = f"{base}/{path}"
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
        if dist is not None and "text/html" in request.headers.get("accept", "").lower():
            # Browser navigation with the daemon down: boot the SPA anyway.
            return Response(
                content=_spa_index(dist),
                status_code=200,
                media_type="text/html; charset=utf-8",
            )
        return JSONResponse(
            status_code=502,
            content={
                "error": "serve-unreachable",
                "url": url,
                "detail": f"{type(exc).__name__}: {str(exc)[:160]}",
            },
        )
    try:  # diagnostics: last 200 proxied calls
        from .state import state_dir

        log = state_dir() / "web-proxy.log"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{request.method} /{path} -> {upstream.status_code}\n")
        if log.stat().st_size > 200_000:
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-500:]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
    media_type = upstream.headers.get("content-type", "application/octet-stream")
    content = upstream.content
    wants_html = "text/html" in request.headers.get("accept", "").lower()
    if dist is not None and wants_html and upstream.status_code >= 400:
        # Browser navigation to a client-side route (/session/<id>, /threads, …):
        # the daemon answers 404 JSON, the SPA must boot instead.
        return Response(
            content=_spa_index(dist),
            status_code=200,
            media_type="text/html; charset=utf-8",
        )
    if "text/html" in media_type.lower():
        if dist is not None:
            content = _spa_index(dist)
            media_type = "text/html; charset=utf-8"
        else:
            content = _rewrite_console_html(content)
    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type=media_type,
    )


__all__ = ["router", "serve_url"]
