# -*- coding: utf-8 -*-
"""网络预检、重试分类、日志开关的回归测试。"""

from __future__ import annotations

import logging
import logging.handlers
import os
import types as pytypes

import httpx
import pytest
from fastapi.testclient import TestClient

import vertex_openai_proxy as vp


def _ns(**kw):
    return pytypes.SimpleNamespace(**kw)


def _resp_text(text):
    part = _ns(text=text, function_call=None, function_response=None)
    return _ns(
        text=text,
        candidates=[_ns(content=_ns(parts=[part]), finish_reason=_ns(name="STOP"))],
        usage_metadata=_ns(
            prompt_token_count=1, candidates_token_count=1, total_token_count=2
        ),
    )


class _FakeModels:
    def __init__(self, script):
        self.script = list(script)
        self.n = 0

    def generate_content(self, model=None, contents=None, config=None):
        self.n += 1
        return self.script.pop(0)

    def generate_content_stream(self, model=None, contents=None, config=None):
        self.n += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return iter(item)


@pytest.fixture
def client():
    vp._SIG_CACHE.clear()
    return TestClient(vp.app)


def test_preflight_refuses_when_local_proxy_down(client, monkeypatch):
    """本地代理端口不通时：直接 502 + 人话，不发起上游调用、不重试。"""
    fake = _ns(models=_FakeModels([_resp_text("should not be called")]))
    monkeypatch.setattr(vp, "get_client", lambda: fake)
    monkeypatch.setenv("PREFLIGHT", "1")
    monkeypatch.setenv("VERTEX_PROXY", "http://127.0.0.1:59999")
    monkeypatch.setattr(vp, "_pf_until", 0.0)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "网络不可达" in msg and "Clash" in msg
    assert fake.models.n == 0, "预检失败时不应发起上游调用，更不该重试"


def test_retryable_classification():
    assert vp._retryable(httpx.ReadTimeout("timed out")) is True
    assert vp._retryable(httpx.RemoteProtocolError("server disconnected")) is True
    assert vp._retryable(Exception("429 RESOURCE_EXHAUSTED")) is True
    assert vp._retryable(Exception("503 UNAVAILABLE")) is True
    assert vp._retryable(httpx.ConnectError("Connection refused")) is False
    assert vp._retryable(Exception("400 INVALID_ARGUMENT")) is False
    assert vp._retryable(Exception("getaddrinfo failed")) is False


def test_upstream_unreachable_message_is_friendly():
    exc = vp.UpstreamUnreachable("本地代理 127.0.0.1:7897 连不上")
    msg = vp._upstream_msg(exc, "gemini-3.8-flash")
    assert msg.startswith("[网络不可达]")
    assert "127.0.0.1:7897" in msg


def test_log_to_file_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(vp, "LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_TO_FILE", "1")
    path = vp.setup_logging()
    assert path and os.path.exists(path)
    vp.log.info("hello-from-test")
    with open(path, encoding="utf-8") as f:
        assert "hello-from-test" in f.read()

    monkeypatch.setenv("LOG_TO_FILE", "0")
    assert vp.setup_logging() == ""
    assert not any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        for h in logging.getLogger().handlers
    )
