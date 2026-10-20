# -*- coding: utf-8 -*-
"""价格估算与「思考 token」计费口径的回归测试。"""

from __future__ import annotations

import json
import types as pytypes

import pytest

import vertex_openai_proxy as vp
from usage import Usage, cost_of, load_prices


def _ns(**kw):
    return pytypes.SimpleNamespace(**kw)


class _NoUsageRegistry:
    """避免测试期间读真实账本/真实价格表。"""


@pytest.fixture
def prices_file(tmp_path, monkeypatch):
    p = tmp_path / "prices.json"
    p.write_text(
        json.dumps({"_note": "示例价", "m-cheap": {"in": 1.0, "out": 2.0}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRICES_FILE", str(p))
    return p


def test_load_prices_skips_notes_and_nulls(prices_file):
    prices = load_prices()
    assert "m-cheap" in prices
    assert "_note" not in prices


def test_cost_of_math():
    prices = {"m": {"in": 1.0, "out": 2.0}}
    assert cost_of(prices, "m", 1_000_000, 0) == 1.0
    assert cost_of(prices, "m", 0, 1_000_000) == 2.0
    assert cost_of(prices, "m", 500_000, 500_000) == 1.5
    assert cost_of(prices, "unknown", 1, 1) is None


def test_stats_includes_cost_and_missing(tmp_path, prices_file):
    u = Usage(data_dir=str(tmp_path / "usage"))
    u.record("m-cheap", 1_000_000, 0)  # 1.0 美元
    u.record("m-unknown", 1000, 1000)  # 没有价格 -> 计入 missing

    data = u.stats(days=1)
    assert data["today"]["cost"] == 1.0
    assert data["total"]["cost"] == 1.0
    assert "m-unknown" in data["missing_prices"]
    assert data["prices_note"] == "示例价"


def test_usage_counts_thinking_tokens_as_output():
    """Google 按含思考的 token 计费：completion 必须把 thoughts 算进去。"""
    um = _ns(
        prompt_token_count=100,
        candidates_token_count=10,
        thoughts_token_count=90,
        total_token_count=200,
    )
    resp = _ns(usage_metadata=um)
    u = vp._usage(resp)
    assert u["prompt_tokens"] == 100
    assert u["completion_tokens"] == 100, "10 正文 + 90 思考"
    assert u["thoughts_token_count"] == 90
    assert u["total_tokens"] == 200
