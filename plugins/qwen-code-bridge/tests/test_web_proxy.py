# -*- coding: utf-8 -*-
"""P3 tests: qwen serve status probe + reverse proxy (fake upstream)."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwen_code_bridge import web
from qwen_code_bridge.router import build_router


class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = f"SERVED GET {self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length).decode("utf-8", "ignore")
        body = f"SERVED POST {self.path} {payload}".encode()
        self.send_response(201)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def upstream(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv(web.SERVE_URL_ENV, url)
    yield url
    server.shutdown()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("QWEN_CODE_BRIDGE_SERVE_AUTOSTART", "0")
    app = FastAPI()
    app.include_router(build_router(), prefix="/api/qwen-code-bridge")
    return TestClient(app)


def test_status_reports_unreachable(monkeypatch, client) -> None:
    monkeypatch.setenv(web.SERVE_URL_ENV, "http://127.0.0.1:9")
    data = client.get("/api/qwen-code-bridge/web/status").json()
    assert data["reachable"] is False
    assert "error" in data


def test_status_ok(upstream, client) -> None:
    # the fake upstream answers any path, including /health
    data = client.get("/api/qwen-code-bridge/web/status").json()
    assert data["reachable"] is True
    assert data["status_code"] == 200


def test_proxy_get(upstream, client) -> None:
    resp = client.get("/api/qwen-code-bridge/web/sessions?limit=2")
    assert resp.status_code == 200
    assert "SERVED GET /sessions?limit=2" in resp.text


def test_proxy_post_with_body(upstream, client) -> None:
    resp = client.post("/api/qwen-code-bridge/web/prompt", json={"text": "hi"})
    assert resp.status_code == 201
    assert "SERVED POST /prompt" in resp.text
    assert 'hi' in resp.text and 'text' in resp.text


def test_proxy_unreachable_returns_502(monkeypatch, client) -> None:
    monkeypatch.setenv(web.SERVE_URL_ENV, "http://127.0.0.1:9")
    resp = client.get("/api/qwen-code-bridge/web/x")
    assert resp.status_code == 502
    assert resp.json()["error"] == "serve-unreachable"


def test_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv(web.SERVE_TOKEN_ENV, "tok-1")
    assert web.serve_token() == "tok-1"
    monkeypatch.delenv(web.SERVE_TOKEN_ENV)
    assert isinstance(web.serve_token(), str)
