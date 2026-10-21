# -*- coding: utf-8 -*-
"""thought_signature 缓存持久化：重启后旧会话仍能续聊。"""

from __future__ import annotations

import json

import vertex_openai_proxy as vp
from google.genai import types


def test_sig_cache_survives_restart(tmp_path, monkeypatch):
    sig_file = tmp_path / "sig.jsonl"
    monkeypatch.setattr(vp, "_SIG_FILE", str(sig_file))
    vp._SIG_CACHE.clear()

    part = types.Part(function_call=types.FunctionCall(name="TodoWrite", args={"a": 1}))
    part.thought_signature = b"\x00\x01\xfe\xffSIG"
    vp._remember_part("call_abc", part)

    assert sig_file.exists(), "签名应落盘"
    line = json.loads(sig_file.read_text(encoding="utf-8").strip())
    assert line["id"] == "call_abc"
    assert "__sig_b64" in line["part"]

    # 模拟进程重启：内存清空后从磁盘恢复
    vp._SIG_CACHE.clear()
    vp._load_sig_cache()

    restored = vp._SIG_CACHE.get(vp._sig_key("call_abc", "vertex"))
    assert restored is not None, "重启后应能恢复"
    assert restored.thought_signature == b"\x00\x01\xfe\xffSIG"
    assert restored.function_call.name == "TodoWrite"
    assert restored.function_call.args == {"a": 1}


def test_missing_signature_is_reused_in_replay(tmp_path, monkeypatch):
    """回放历史时，用恢复出来的签名替换重建的 part。"""
    sig_file = tmp_path / "sig.jsonl"
    monkeypatch.setattr(vp, "_SIG_FILE", str(sig_file))
    vp._SIG_CACHE.clear()

    part = types.Part(
        function_call=types.FunctionCall(name="Read", args={"path": "a.py"})
    )
    part.thought_signature = b"SIG-1"
    vp._remember_part("call_1", part)

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "content"},
    ]
    _system, contents = vp.build_contents(messages)
    model_turn = [c for c in contents if c.role == "model"][0]
    assert model_turn.parts[0].thought_signature == b"SIG-1"


def test_signatures_are_namespaced_per_backend(tmp_path, monkeypatch):
    """Vertex 产出的签名不能喂给 AI Studio，两边必须分开存。"""
    monkeypatch.setattr(vp, "_SIG_FILE", str(tmp_path / "sig.jsonl"))
    vp._SIG_CACHE.clear()

    part = types.Part(function_call=types.FunctionCall(name="Read", args={}))
    part.thought_signature = b"VERTEX-SIG"
    vp._remember_part("call_x", part, backend="vertex")

    # 同一 call_id 在 AI Studio 侧不应命中 Vertex 的签名
    assert vp._SIG_CACHE.get(vp._sig_key("call_x", "aistudio")) is None
    assert vp._SIG_CACHE.get(vp._sig_key("call_x", "vertex")) is not None

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_x", "content": "x"},
    ]
    _s, contents = vp.build_contents(messages, backend="aistudio")
    model_turn = [c for c in contents if c.role == "model"][0]
    assert not getattr(model_turn.parts[0], "thought_signature", None)
