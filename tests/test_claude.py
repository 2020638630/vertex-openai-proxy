# -*- coding: utf-8 -*-
"""claude_adapter 与 claude-* 路由的回归测试（全部假上游，不联网）。"""

from __future__ import annotations

import json
import types as pytypes

import pytest
from fastapi.testclient import TestClient

import claude_adapter as ca
import vertex_openai_proxy as vp


def _ns(**kw):
    return pytypes.SimpleNamespace(**kw)


def _block_text(text):
    return _ns(type="text", text=text)


def _block_tool(tid, name, args):
    return _ns(type="tool_use", id=tid, name=name, input=args)


def _resp(blocks, stop="end_turn"):
    return _ns(
        content=blocks,
        stop_reason=stop,
        usage=_ns(input_tokens=10, output_tokens=5),
    )


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "执行命令",
            "parameters": {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }
]


# --------------------------------------------------------------------------
# 纯转换
# --------------------------------------------------------------------------
def test_build_messages_with_tools_and_tool_result():
    body = {
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "看下目录"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "a.py"},
            {"role": "user", "content": "有几个文件？"},
        ]
    }
    system, msgs = ca.build_messages(body["messages"])
    assert system == "你是助手"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "user"]
    # assistant 里应有 text? 这里 content 为 None，只有 tool_use
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[1]["content"][0]["input"] == {"command": "ls"}
    assert msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][0]["tool_use_id"] == "call_1"


def test_build_messages_merges_same_role_and_starts_with_user():
    system, msgs = ca.build_messages(
        [
            {"role": "assistant", "content": "先说话"},
            {"role": "user", "content": "一"},
            {"role": "user", "content": "二"},
        ]
    )
    assert msgs[0]["role"] == "user"  # 首条必须是 user
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert len(msgs[2]["content"]) == 2  # 同角色合并


def test_build_tools_strips_unsupported_schema_keys():
    tools, choice = ca.build_tools({"tools": TOOLS, "tool_choice": "required"})
    assert tools[0]["name"] == "Bash"
    # $schema 这类元字段要剔掉；additionalProperties 是合法 JSON Schema，Anthropic 接受，予以保留
    assert "$schema" not in tools[0]["input_schema"]
    assert tools[0]["input_schema"]["type"] == "object"
    assert tools[0]["input_schema"]["additionalProperties"] is False
    assert choice == {"type": "any"}


def test_to_openai_text_and_tool_use():
    resp = _resp(
        [_block_text("好的"), _block_tool("toolu_1", "Bash", {"command": "ls"})],
        "tool_use",
    )
    out = ca.to_openai(resp, "claude-sonnet-4-5@20250929", "chatcmpl-x", 1)
    msg = out["choices"][0]["message"]
    assert msg["content"] == "好的"
    assert msg["tool_calls"][0]["id"] == "toolu_1"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {
        "command": "ls"
    }
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"]["total_tokens"] == 15


@pytest.mark.parametrize(
    "reason,has_tools,expected",
    [
        ("end_turn", False, "stop"),
        ("max_tokens", False, "length"),
        ("tool_use", True, "tool_calls"),
        (None, False, "stop"),
    ],
)
def test_normalize_stop(reason, has_tools, expected):
    assert ca.normalize_stop(reason, has_tools) == expected


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@pytest.fixture
def client():
    vp._SIG_CACHE.clear()
    return TestClient(vp.app)


def test_route_claude_non_stream(client, monkeypatch):
    monkeypatch.setattr(ca, "get_client", lambda: object())
    monkeypatch.setattr(ca, "create", lambda c, kw: _resp([_block_text("claude ok")]))

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-4-5@20250929",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "claude ok"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_route_claude_stream(client, monkeypatch):
    monkeypatch.setattr(ca, "get_client", lambda: object())

    def fake_stream(client_, kwargs):
        yield {"type": "text", "text": "claude "}
        yield {"type": "text", "text": "stream ok"}
        yield {"type": "finish", "reason": "stop"}

    monkeypatch.setattr(ca, "iter_stream", fake_stream)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-4-5@20250929",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    text = r.text
    assert "claude " in text and "stream ok" in text
    assert '"finish_reason": "stop"' in text
    assert text.strip().endswith("[DONE]")


def test_route_claude_429_gives_readable_error(client, monkeypatch):
    monkeypatch.setattr(ca, "get_client", lambda: object())

    def boom(c, kw):
        raise Exception("Error code: 429 - quota exceeded")

    monkeypatch.setattr(ca, "create", boom)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-4-5@20250929",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 502
    assert "配额" in r.json()["error"]["message"]


def test_route_claude_sanitizes_and_restores(client, monkeypatch):
    captured = {}

    def fake_create(c, kw):
        captured["kw"] = kw
        # 模拟模型把占位符原样回显
        return _resp([_block_text("DB_PASSWORD=[[SECRET_1]]")])

    monkeypatch.setattr(ca, "get_client", lambda: object())
    monkeypatch.setattr(ca, "create", fake_create)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-4-5@20250929",
            "messages": [{"role": "user", "content": "DB_PASSWORD=Passw0rd!2026"}],
        },
    )
    assert r.status_code == 200
    sent = json.dumps(captured["kw"]["messages"], ensure_ascii=False)
    assert "Passw0rd" not in sent, "明文不能发给 Claude"
    assert "[[SECRET_1]]" in sent
    assert r.json()["choices"][0]["message"]["content"] == "DB_PASSWORD=Passw0rd!2026"
