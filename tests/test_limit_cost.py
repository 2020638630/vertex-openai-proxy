# -*- coding: utf-8 -*-
"""按「花费」限额：比 token 数量更合理（同样 45M token，各家价差 30 倍）。"""

from __future__ import annotations

import pytest

from usage import BudgetExceeded, Usage


@pytest.fixture
def u(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMIT_MODE", "block")
    monkeypatch.setenv("RPM_LIMIT", "0")
    monkeypatch.setenv("DAILY_REQ_LIMIT", "0")
    monkeypatch.setenv("DAILY_TOKEN_LIMIT", "0")
    monkeypatch.setenv("DAILY_UNCACHED_LIMIT", "0")
    monkeypatch.setenv("DAILY_COST_LIMIT", "1.0")
    monkeypatch.setenv("LIMIT_HARD_MULTIPLIER", "0")
    return Usage(data_dir=str(tmp_path))


def test_cost_is_accumulated_on_record(u):
    u.record("gemini-4-argon", 1_000_000, 0)  # $4/M 输入 -> $4
    assert u.snapshot()["cost"] == pytest.approx(4.0, rel=0.01)


def test_cost_limit_blocks_new_session(u):
    u.record("gemini-4-argon", 1_000_000, 0)
    with pytest.raises(BudgetExceeded) as ei:
        u.check(new_session=True)
    assert "花费" in str(ei.value)


def test_cost_limit_soft_lands_mid_session(u):
    u.record("gemini-4-argon", 1_000_000, 0)
    problems = u.check(new_session=False)
    assert any("花费" in p for p in problems)


def test_cost_limit_zero_disables(u):
    u.daily_cost = 0
    u.record("gemini-4-argon", 1_000_000, 0)
    assert u.check(new_session=True) == []


def test_free_model_costs_nothing(u):
    u.record("openrouter/whatever:free", 5_000_000, 1_000_000)
    assert u.snapshot()["cost"] == 0.0
    assert u.check(new_session=True) == []


def test_hard_fuse_on_cost(u):
    u.daily_cost = 1.0
    u.hard_mult = 3.0
    u.record("gemini-4-argon", 1_000_000, 0)  # $4，超 3 倍
    with pytest.raises(BudgetExceeded) as ei:
        u.check(new_session=False)
    assert "硬阈值" in str(ei.value)
