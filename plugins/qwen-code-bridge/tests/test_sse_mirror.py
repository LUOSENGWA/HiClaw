# -*- coding: utf-8 -*-
"""SSE run stream + chat mirror + bridge prompt section."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwen_code_bridge import chats, prompt
from qwen_code_bridge.router import build_router
from qwen_code_bridge.slash import append_to_session


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_CODE_BRIDGE_STATE_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(build_router(), prefix="/api/qwen-code-bridge")
    return TestClient(app), tmp_path


def test_sse_stream_emits_log_state_end(client) -> None:
    http, tmp = client
    task_id = "a" * 32
    d = tmp / "dispatch"
    d.mkdir(parents=True)
    (d / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "state": "done", "exit_code": 0, "wall_s": 2.0}),
        encoding="utf-8",
    )
    (d / f"{task_id}.log").write_text("line-one\nline-two\n", encoding="utf-8")
    with http.stream("GET", f"/api/qwen-code-bridge/runs/{task_id}/stream") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert '"type": "log"' in body and "line-one" in body
    assert '"type": "state"' in body and '"done"' in body
    assert '"type": "end"' in body


def test_mirror_run_payload(monkeypatch) -> None:
    calls = []

    def fake_request(method, url, body, agent_id, timeout=5.0):  # noqa: ARG001
        calls.append((method, url, body, agent_id))
        return {"appended": True}

    monkeypatch.setattr(chats, "_request", fake_request)
    ok = chats.mirror_run("http://app", "agent-x", "b" * 32, "[running] hello", "u1")
    assert ok is True
    method, url, body, agent = calls[0]
    assert method == "POST"
    assert url.endswith(f"/api/qwen-code-bridge/runs/{'b' * 32}/mirror")
    assert body == {"text": "[running] hello", "user_id": "u1"}
    assert agent == "agent-x"


@pytest.mark.asyncio
async def test_append_to_session_roles() -> None:
    saved = {}

    class FakeSession:
        async def get_session_state_dict(self, session_id, user_id="", channel=""):
            return {}

        async def update_session_state(self, session_id, key, value, user_id="", channel=""):
            saved.update(
                {
                    "session_id": session_id,
                    "key": key,
                    "value": value,
                    "user_id": user_id,
                    "channel": channel,
                }
            )

    ok = await append_to_session(
        FakeSession(),
        "qwen-code-bridge:" + "c" * 32,
        "default",
        "qwen-code-bridge",
        role="assistant",
        name="qwen-code",
        text="[running] x",
    )
    assert ok is True
    ctx = saved["value"]["context"]
    assert ctx[-1]["role"] == "assistant"
    assert ctx[-1]["content"][0]["text"] == "[running] x"


def test_bridge_prompt_content() -> None:
    text = prompt.bridge_prompt()
    assert "qwen-code-bridge" in text
    assert "/qcode" in text
    assert "STRICT→plan" in text
