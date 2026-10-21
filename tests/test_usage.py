# -*- coding: utf-8 -*-
"""用量记账与限额守卫的回归测试。"""

from __future__ import annotations

import json
import os

import pytest

from usage import BudgetExceeded, Usage


@pytest.fixture
def u(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMIT_MODE", "warn")
    monkeypatch.setenv("RPM_LIMIT", "30")
    monkeypatch.setenv("DAILY_REQ_LIMIT", "500")
    monkeypatch.setenv("DAILY_TOKEN_LIMIT", "2000000")
    monkeypatch.setenv("DAILY_UNCACHED_LIMIT", "2000000")
    monkeypatch.setenv("LIMIT_HARD_MULTIPLIER", "0")  # 测试里默认关掉熔断
    return Usage(data_dir=str(tmp_path))


def test_record_writes_jsonl_and_counts(u, tmp_path):
    u.record("gemini-3.8-flash", 100, 20, ms=1234, tools=2)
    u.record("gemini-4-argon", 50, 10, ms=800, ok=False, error="boom")

    snap = u.snapshot()
    assert snap["requests"] == 2
    assert snap["fail"] == 1
    assert snap["tokens_in"] == 150
    assert snap["tokens_out"] == 30
    assert snap["by_model"]["gemini-3.8-flash"] == 1

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["in"] == 100


def test_reload_after_restart(u, tmp_path):
    u.record("m1", 10, 5)
    u.record("m1", 20, 5)

    again = Usage(data_dir=str(tmp_path))
    snap = again.snapshot()
    assert snap["requests"] == 2, "重启后应能从当天账本恢复计数"
    assert snap["tokens_total"] == 40


def test_warn_mode_does_not_block(u, monkeypatch):
    monkeypatch.setenv("DAILY_TOKEN_LIMIT", "10")
    u.daily_tokens = 10
    u.record("m", 100, 0)

    problems = u.check()  # 不应抛异常
    assert problems and "tokens" in problems[0]


def test_block_mode_raises_with_retry_after(u):
    u.mode = "block"
    u.daily_tokens = 10
    u.record("m", 100, 0)

    # 软着陆：新会话开始才拦
    with pytest.raises(BudgetExceeded) as ei:
        u.check(new_session=True)
    assert ei.value.retry_after > 0
    assert "token" in str(ei.value)


def test_soft_landing_lets_current_session_continue(u):
    u.mode = "block"
    u.daily_tokens = 10
    u.record("m", 100, 0)

    problems = u.check(new_session=False)  # 当前会话中途：只预警，不抛异常
    assert problems, "超限时应返回问题描述供日志使用"


def test_uncached_metric_ignores_cache_hits(u):
    u.mode = "block"
    u.daily_uncached = 1000
    u.daily_tokens = 10_000_000
    # 输入 9000，但 8500 命中缓存 -> 未命中只有 500，不该拦
    u.record("m", 9000, 10, cached=8500)
    assert u.check(new_session=True) == []


def test_hard_multiplier_forces_block_mid_session(u):
    u.mode = "block"
    u.daily_tokens = 100
    u.hard_mult = 3.0
    u.record("m", 400, 0)  # 已超 4 倍

    with pytest.raises(BudgetExceeded) as ei:
        u.check(new_session=False)
    assert "硬阈值" in str(ei.value)


def test_rpm_window(u):
    u.mode = "block"
    u.rpm = 2
    u.check()
    u.check()
    with pytest.raises(BudgetExceeded) as ei:
        u.check()
    assert "60 秒" in str(ei.value) or "请求数" in str(ei.value)


def test_stats_aggregates_days(u):
    u.record("gemini-3.8-flash", 100, 50)
    data = u.stats(days=3)
    assert data["total"]["requests"] >= 1
    assert data["total"]["tokens"] >= 150
    assert data["daily"][0]["date"] == u.snapshot()["date"]
    assert "limits" in data["today"]
