# -*- coding: utf-8 -*-
"""额度软着陆：当前会话放行，下一段会话开始才拦。"""

from __future__ import annotations

import types as pytypes

import pytest
from fastapi.testclient import TestClient

import vertex_openai_proxy as vp
from usage import Usage


def _ns(**kw):
    return pytypes.SimpleNamespace(**kw)


class _FakeModels:
    def generate_content(self, model=None, contents=None, config=None):
        part = _ns(text="ok", function_call=None, function_response=None)
        return _ns(
            text="ok",
            candidates=[_ns(content=_ns(parts=[part]), finish_reason=_ns(name="STOP"))],
            usage_metadata=_ns(
                prompt_token_count=1,
                candidates_token_count=1,
                thoughts_token_count=0,
                total_token_count=2,
                cached_content_token_count=0,
            ),
        )


@pytest.fixture
def client(monkeypatch, tmp_path):
    u = Usage(data_dir=str(tmp_path / "usage"))
    u.mode = "block"
    u.daily_tokens = 10  # 已用 100 > 10，处于超限状态
    u.daily_uncached = 10
    u.hard_mult = 0  # 关掉硬熔断，专测软着陆
    u.record("gemini-3.8-flash", 100, 0)

    monkeypatch.setattr(vp, "USAGE", u)
    monkeypatch.setattr(vp, "get_client", lambda: _ns(models=_FakeModels()))
    vp._SIG_CACHE.clear()
    return TestClient(vp.app)


def _body(first_user: str, extra: int = 0):
    msgs = [{"role": "user", "content": first_user}]
    for i in range(extra):
        msgs.append({"role": "assistant", "content": "ok"})
        msgs.append({"role": "user", "content": "继续 %d" % i})
    return {"model": "gemini-3.8-flash", "messages": msgs, "max_tokens": 32}


def test_new_session_is_blocked_when_over_limit(client):
    r = client.post("/v1/chat/completions", json=_body("第一轮评审"))
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}
    assert "额度" in r.json()["error"]["message"]


def test_ongoing_session_keeps_working(client):
    # 先开一段会话（超限，会被拦）
    assert (
        client.post("/v1/chat/completions", json=_body("第一轮评审")).status_code == 429
    )

    # 同一个首条消息 = 同一会话；但首条就被拦了，换个场景：
    u = vp.USAGE
    u.daily_tokens = 10_000  # 先抬高限额，模拟"会话中途才超限"
    u.daily_uncached = 10_000
    first = client.post("/v1/chat/completions", json=_body("第一轮评审"))
    assert first.status_code == 200

    # 会话继续（首条消息相同，历史变长）
    u.daily_tokens = 10  # 此刻又超限了
    u.daily_uncached = 10
    cont = client.post("/v1/chat/completions", json=_body("第一轮评审", extra=1))
    assert cont.status_code == 200, "当前会话中途不应被掐断"


def test_next_conversation_is_blocked_again(client):
    u = vp.USAGE
    u.daily_tokens = 10_000
    u.daily_uncached = 10_000
    assert (
        client.post("/v1/chat/completions", json=_body("第一轮评审")).status_code == 200
    )
    u.daily_tokens = 10
    u.daily_uncached = 10

    # 换一个首条消息 = 新会话 -> 拦
    r = client.post("/v1/chat/completions", json=_body("第二轮评审"))
    assert r.status_code == 429
