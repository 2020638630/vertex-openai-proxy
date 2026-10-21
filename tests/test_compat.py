# -*- coding: utf-8 -*-
"""通用 OpenAI 兼容通道：路由、payload、usage 归一、路由级转发。"""

from __future__ import annotations

import types as pytypes

import pytest
from fastapi.testclient import TestClient

import openai_compat as compat
import vertex_openai_proxy as vp

PROVIDERS = {
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": "",
        "models": ["qwen2.5:7b"],
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat"],
    },
    "relay": {"base_url": "", "api_key_env": "RELAY_API_KEY", "models": ["whatever"]},
}


def test_resolve_by_prefix():
    name, model, cfg = compat.resolve("ollama/qwen2.5:7b", PROVIDERS)
    assert name == "ollama"
    assert model == "qwen2.5:7b"  # 前缀被剥掉，只把模型名发给上游
    assert cfg["base_url"].startswith("http://127.0.0.1")


def test_resolve_by_models_list():
    name, model, _ = compat.resolve("deepseek-chat", PROVIDERS)
    assert (name, model) == ("deepseek", "deepseek-chat")


def test_resolve_unknown_falls_through():
    assert compat.resolve("gemini-3.8-flash", PROVIDERS) is None


def test_resolve_skips_unconfigured_provider():
    # relay 的 base_url 是空的，不应劫持请求
    assert compat.resolve("relay/whatever", PROVIDERS) is None


def test_build_payload_whitelist_and_stream_options():
    body = {
        "model": "ollama/qwen2.5:7b",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.2,
        "some_client_only_field": 1,
        "stream": True,
    }
    p = compat.build_payload(body, "qwen2.5:7b")
    assert p["model"] == "qwen2.5:7b"
    assert p["stream"] is True
    assert p["stream_options"] == {"include_usage": True}
    assert "some_client_only_field" not in p


@pytest.mark.parametrize(
    "usage,expected",
    [
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
            {"in": 100, "out": 20, "cached": 80},
        ),
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 70,
            },
            {"in": 100, "out": 20, "cached": 70},
        ),
        (
            {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 60},
            {"in": 100, "out": 20, "cached": 60},
        ),
        (None, {"in": 0, "out": 0, "cached": 0}),
    ],
)
def test_map_usage_variants(usage, expected):
    assert compat.map_usage(usage) == expected


# --------------------------------------------------------------------------
# 路由级
# --------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    original = compat.resolve
    monkeypatch.setattr(compat, "load_providers", lambda: PROVIDERS)
    monkeypatch.setattr(compat, "resolve", lambda m: original(m, PROVIDERS))
    vp._SIG_CACHE.clear()
    return TestClient(vp.app)


def test_route_compat_non_stream_restores_and_records(client, monkeypatch):
    def fake_post(cfg, payload):
        assert payload["model"] == "qwen2.5:7b"
        return {
            "id": "x",
            "object": "chat.completion",
            "model": "qwen2.5:7b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "密码是 [[SECRET_1]]"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        }

    monkeypatch.setattr(compat, "post_json", fake_post)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "ollama/qwen2.5:7b",
            "messages": [{"role": "user", "content": "DB_PASSWORD=Passw0rd!2026 是啥"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "ollama/qwen2.5:7b"  # 模型名改回客户端认知的名字
    assert "Passw0rd!2026" in body["choices"][0]["message"]["content"]


def test_route_compat_stream(client, monkeypatch):
    def fake_iter(cfg, payload):
        yield {
            "choices": [{"index": 0, "delta": {"content": "你"}, "finish_reason": None}]
        }
        yield {
            "choices": [
                {"index": 0, "delta": {"content": "好"}, "finish_reason": "stop"}
            ]
        }

    monkeypatch.setattr(compat, "iter_chunks", fake_iter)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "ollama/qwen2.5:7b",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    text = r.text
    assert "你" in text and "好" in text
    assert text.strip().endswith("[DONE]")


def test_route_compat_connect_error_is_friendly(client, monkeypatch):
    import httpx

    def boom(cfg, payload):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(compat, "post_json", boom)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "ollama/qwen2.5:7b",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "Ollama" in msg or "连不上" in msg
