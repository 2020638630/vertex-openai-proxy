# -*- coding: utf-8 -*-
"""MCP 任务卡体检工具的规则层测试（不联网、不花钱）。"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp 未安装（pip install mcp）")

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "criteria_mcp", ROOT / "tools" / "criteria_mcp.py"
)
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)


def _kinds(result):
    return {f["kind"] for f in result["findings"]}


def test_vague_words_are_flagged():
    r = cm._lint("把页面改得美观一些，间距适当调整，尽量优雅。")
    assert "模糊表述" in _kinds(r)
    detail = [f for f in r["findings"] if f["kind"] == "模糊表述"][0]["detail"]
    assert "美观" in detail and "适当" in detail


def test_missing_negative_constraint_is_flagged():
    r = cm._lint("实现一个合并区间的函数，返回合并后的列表，写完整代码。")
    assert "缺少禁止项" in _kinds(r)


def test_missing_acceptance_is_flagged():
    r = cm._lint("实现一个合并区间的函数，不要引入新依赖。")
    assert "缺少验收标准" in _kinds(r)


def test_good_task_card_has_few_findings():
    text = (
        "实现 merge_intervals(intervals)，要求：\n"
        "1. 输入 [[1,3],[2,6]] 输出 [[1,6]]\n"
        "2. 相邻区间（5 与 6）必须合并\n"
        "3. 不要引入任何第三方依赖，只用标准库\n"
        "验收：pytest tests/test_merge.py 全绿，且新增 3 个用例。\n"
    )
    r = cm._lint(text)
    assert "模糊表述" not in _kinds(r)
    assert "缺少禁止项" not in _kinds(r)
    assert "缺少验收标准" not in _kinds(r)


def test_read_input_accepts_path_and_inline(tmp_path):
    f = tmp_path / "card.md"
    f.write_text("内容", encoding="utf-8")
    text, src = cm._read_input(str(f))
    assert text == "内容" and src.endswith("card.md")

    text2, src2 = cm._read_input("这是一段足够长的内联正文" * 20)
    assert src2 == "(inline text)" and len(text2) > 100
