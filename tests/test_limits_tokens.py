# -*- coding: utf-8 -*-
"""max_tokens 截断与重复字段清理（2026-10-21 两个 400 的回归测试）。"""

from __future__ import annotations

import openai_compat as compat
import vertex_openai_proxy as vp


def test_clamp_caps_to_model_limit():
    # 3.8-flash 实测上限 65536，客户端给 128000 必须被截断
    assert vp.clamp_output_tokens(128000, "gemini-3.8-flash") == 65536
    # 未超限则原样返回
    assert vp.clamp_output_tokens(8000, "gemini-3.8-flash") == 8000
    # 未核实上限的模型不瞎截断
    assert vp.clamp_output_tokens(999999, "gemini-4-argon") == 999999
    # 非法值原样返回
    assert vp.clamp_output_tokens(None, "gemini-3.8-flash") is None


def test_build_payload_drops_duplicate_token_field():
    """客户端同时发 max_tokens 和 max_completion_tokens，上游会 400。"""
    body = {
        "model": "aistudio/gemini-3.1-pro-preview",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8000,
        "max_completion_tokens": 128000,
    }
    p = compat.build_payload(body, "gemini-3.1-pro-preview")
    assert "max_completion_tokens" not in p
    assert p["max_tokens"] == 8000  # 优先保留 max_tokens


def test_build_payload_maps_completion_tokens_when_alone():
    body = {
        "model": "aistudio/gemini-3.1-pro-preview",
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 4096,
    }
    p = compat.build_payload(body, "gemini-3.1-pro-preview")
    assert p["max_tokens"] == 4096
    assert "max_completion_tokens" not in p


def test_upstream_error_body_is_surfaced(monkeypatch):
    """上游 400 的原因必须出现在错误信息里，否则只看到状态码。"""

    class _Resp:
        status_code = 400
        text = '{"error":{"message":"max_tokens and max_completion_tokens cannot both be set"}}'
        request = None

    try:
        compat._check(_Resp())
    except Exception as exc:
        assert "cannot both be set" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("400 应当抛异常")
