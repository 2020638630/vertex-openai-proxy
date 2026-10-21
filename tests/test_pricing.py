# -*- coding: utf-8 -*-
"""价格估算、「思考 token」与「缓存折扣」计费口径的回归测试。"""

from __future__ import annotations

import json
import types as pytypes

import pytest

import vertex_openai_proxy as vp
from usage import Usage, cache_discount, cost_of, load_prices


def _ns(**kw):
    return pytypes.SimpleNamespace(**kw)


@pytest.fixture
def prices_file(tmp_path, monkeypatch):
    p = tmp_path / "prices.json"
    p.write_text(
        json.dumps(
            {
                "_note": "示例价",
                "_cache_discount": 0.9,
                "m-cheap": {"in": 1.0, "out": 2.0},
                "m-pricey": {"in": 10.0, "out": 0.0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRICES_FILE", str(p))
    return p


def test_load_prices_skips_meta_keys(prices_file):
    prices = load_prices()
    assert set(prices) == {"m-cheap", "m-pricey"}
    assert cache_discount() == 0.9


def test_cost_of_math():
    prices = {"m": {"in": 1.0, "out": 2.0}}
    assert cost_of(prices, "m", 1_000_000, 0) == 1.0
    assert cost_of(prices, "m", 0, 1_000_000) == 2.0
    assert cost_of(prices, "m", 500_000, 500_000) == 1.5
    assert cost_of(prices, "unknown", 1, 1) is None


def test_cost_of_applies_cache_discount():
    prices = {"m": {"in": 10.0, "out": 0.0}}
    # 全部命中缓存 -> 只按 10% 计
    assert cost_of(
        prices, "m", 1_000_000, 0, cached=1_000_000, discount=0.9
    ) == pytest.approx(1.0)
    # 一半命中 -> 0.5M*10 + 0.5M*1
    assert cost_of(
        prices, "m", 1_000_000, 0, cached=500_000, discount=0.9
    ) == pytest.approx(5.5)
    # cached 超过 in 时按 in 截断，不会出现负成本
    assert cost_of(prices, "m", 1000, 0, cached=99999, discount=0.9) == pytest.approx(
        0.001
    )


def test_stats_reports_cost_and_cache_ratio(tmp_path, prices_file):
    u = Usage(data_dir=str(tmp_path / "usage"))
    u.record("m-cheap", 1_000_000, 0, cached=1_000_000)
    u.record("m-unknown", 1000, 1000)

    data = u.stats(days=1)
    assert data["today"]["cost"] == 0.1  # 全部命中，打一折
    assert data["today"]["cache_hit_ratio"] >= 0.99  # 另有一条 1000 token 未命中
    assert data["total"]["cached"] == 1_000_000
    assert "m-unknown" in data["missing_prices"]
    assert data["prices_note"] == "示例价"


def test_usage_counts_thinking_tokens_as_output():
    """Google 按含思考的 token 计费：completion 必须把 thoughts 算进去。"""
    um = _ns(
        prompt_token_count=100,
        candidates_token_count=10,
        thoughts_token_count=90,
        total_token_count=200,
        cached_content_token_count=60,
    )
    resp = _ns(usage_metadata=um)
    u = vp._usage(resp)
    assert u["prompt_tokens"] == 100
    assert u["completion_tokens"] == 100, "10 正文 + 90 思考"
    assert u["thoughts_token_count"] == 90
    assert u["cached_tokens"] == 60
    assert u["total_tokens"] == 200
