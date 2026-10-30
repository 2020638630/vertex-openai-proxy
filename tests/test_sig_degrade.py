# -*- coding: utf-8 -*-
"""缺 thought_signature 时的降级策略（A）与文本工具调用检测（B）。"""

from __future__ import annotations

import vertex_openai_proxy as vp


def _history(call_id: str = "call_1"):
    return [
        {"role": "user", "content": "读一下 a.py"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "print(1)"},
    ]


def test_missing_signature_degrades_to_text_for_gemini3():
    """3/4 系缺签名：调用与结果都变文本，绝不重建裸 functionCall。"""
    vp._SIG_CACHE.clear()
    _system, contents = vp.build_contents(
        _history(), backend="vertex", require_sig=True
    )

    parts = [p for c in contents for p in (c.parts or [])]
    assert not any(
        getattr(p, "function_call", None) for p in parts
    ), "不该有裸 functionCall"
    assert not any(
        getattr(p, "function_response", None) for p in parts
    ), "不该有孤儿 functionResponse"
    texts = " ".join(getattr(p, "text", "") or "" for p in parts)
    assert "此前的工具调用" in texts
    assert "此前的工具结果" in texts


def test_gemini25_keeps_function_call_when_signature_missing():
    """2.5 系不需要签名，保持原来的 functionCall 行为。"""
    vp._SIG_CACHE.clear()
    _system, contents = vp.build_contents(
        _history(), backend="vertex", require_sig=False
    )

    parts = [p for c in contents for p in (c.parts or [])]
    assert any(getattr(p, "function_call", None) for p in parts)
    assert any(getattr(p, "function_response", None) for p in parts)


def test_signature_present_uses_original_part():
    from google.genai import types

    vp._SIG_CACHE.clear()
    part = types.Part(
        function_call=types.FunctionCall(name="Read", args={"path": "a.py"})
    )
    part.thought_signature = b"SIG-OK"
    vp._remember_part("call_1", part, backend="vertex")

    _system, contents = vp.build_contents(
        _history(), backend="vertex", require_sig=True
    )
    parts = [p for c in contents for p in (c.parts or [])]
    calls = [p for p in parts if getattr(p, "function_call", None)]
    assert calls and calls[0].thought_signature == b"SIG-OK"


def test_missing_signature_logs_ids_and_dedupes(caplog):
    """告警要能看出是哪个调用、什么工具；同一批只告警一次，避免每轮刷屏。"""
    import logging

    vp._SIG_CACHE.clear()
    vp._last_missed_key = ""
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        vp.build_contents(_history(), backend="vertex", require_sig=True)
        vp.build_contents(_history(), backend="vertex", require_sig=True)

    warns = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "缺 thought_signature" in r.getMessage()
    ]
    assert len(warns) == 1, "同一批缺失只应告警一次"
    msg = warns[0].getMessage()
    assert "call_1" in msg and "Read" in msg, "告警里要带上调用 id 与工具名"


def test_text_toolcall_detector():
    assert vp.looks_like_text_toolcall(
        "让我们看看。 call:default_api:Bash{command:pyinstaller --version}"
    )
    assert vp.looks_like_text_toolcall("default_api:Bash{command:ls}")
    # 正常解释性文字不该误判
    assert not vp.looks_like_text_toolcall("我先读一下文件，然后运行测试。")
    assert not vp.looks_like_text_toolcall("")
