# -*- coding: utf-8 -*-
"""任务卡/提示词体检 MCP 服务（stdio）。

给会话（ZCode 等）自行调用的三个工具：

- lint_prompt     零成本规则检查：模糊词、缺验收标准、缺禁止项
- review_criteria 走本地代理做「歧义分析」（花钱，默认 deepseek-flash）
- get_usage       查今日花费与限额水位（零成本，防止撞限额）

设计要点：
- 全部经 http://127.0.0.1:4000 转发，因此自动计入账本、受限额保护
- 只输出建议，不改用户原文；是否采纳由会话/人决定
- stdio 协议下日志一律走 stderr，绝不污染 stdout
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

PROXY_BASE = (os.getenv("PROXY_BASE") or "http://127.0.0.1:4000").rstrip("/")
DEFAULT_MODEL = os.getenv("CRITERIA_MODEL") or "deepseek/deepseek-flash"
MAX_CHARS = 200_000  # 单次送入模型的文本上限，超了截断并提示

mcp = FastMCP("prompt-criteria")

# ---------------------------------------------------------------- 规则检查

# 模糊词：模型看到这些词只能自由发挥，属于"不可验收"的表述
VAGUE_WORDS = [
    "适当",
    "尽量",
    "酌情",
    "合理",
    "优雅",
    "美观",
    "友好",
    "简洁",
    "差不多",
    "若干",
    "一些",
    "等等",
    "之类的",
    "最好",
    "建议",
    "尽快",
    "优化一下",
    "完善一下",
    "丰富一下",
    "大概",
]

# 缺少约束的信号：一份好的任务卡通常要说明"不要做什么"
NEGATIVE_HINTS = ["不要", "禁止", "不允许", "不得", "别改", "不许"]

# 缺少验收的信号
ACCEPT_HINTS = ["验收", "判定", "检查", "测试", "必须", "断言", "pass"]


def _lint(text: str) -> Dict[str, Any]:
    """纯规则检查，不发任何请求。"""
    findings: List[Dict[str, Any]] = []

    hits = []
    for w in VAGUE_WORDS:
        cnt = text.count(w)
        if cnt:
            hits.append("%s×%d" % (w, cnt))
    if hits:
        findings.append(
            {
                "level": "warn",
                "kind": "模糊表述",
                "detail": "、".join(hits),
                "why": "这类词没有判定标准，模型只能自行猜测，容易返工",
                "fix": "换成具体数值或可验证的行为，例如「间距 16px→20px」",
            }
        )

    if not any(k in text for k in NEGATIVE_HINTS):
        findings.append(
            {
                "level": "warn",
                "kind": "缺少禁止项",
                "detail": "全文未出现「不要/禁止/不允许」类约束",
                "why": "只写要做什么、不写不能做什么，模型容易顺手改到别处",
                "fix": "补一句「不要修改 X / 不要引入新依赖」",
            }
        )

    if not any(k in text for k in ACCEPT_HINTS):
        findings.append(
            {
                "level": "warn",
                "kind": "缺少验收标准",
                "detail": "全文未出现「验收/检查/必须/测试」类字样",
                "why": "没有判定标准时，交付物是否合格只能靠感觉",
                "fix": "补 1~3 条可勾选的验收项",
            }
        )

    if len(text) < 200:
        findings.append(
            {
                "level": "info",
                "kind": "篇幅偏短",
                "detail": "%d 字符" % len(text),
                "why": "过短的任务描述往往省略了约束与上下文",
                "fix": "确认是否遗漏了背景、范围、验收",
            }
        )

    return {
        "chars": len(text),
        "findings": findings,
        "verdict": (
            "有 %d 处可改进" % len(findings) if findings else "规则层面未发现问题"
        ),
    }


# ---------------------------------------------------------------- 模型分析

CRITERIA_PROMPT = """你是资深研发负责人，正在评审一份「任务卡 / 需求文档」。

请站在**接活的人**的角度，找出其中**至少 3 处**会导致理解分歧的表述。
如果确实不足 3 处，宁可少写也不要编造。

对每一处，严格按下面格式输出（不要打分、不要客套）：

### N. 原文：「……」
- 解读A：……
- 解读B：……
- 工程隐患：……（会导致什么后果）
- 建议改写：「……」（一句可直接替换的表述）

只输出上述内容，不要写引言和总结。"""


def _read_input(text_or_path: str) -> tuple[str, str]:
    """参数既可以是正文，也可以是文件路径。"""
    p = Path(text_or_path)
    if len(text_or_path) < 400 and p.exists() and p.is_file():
        return p.read_text(encoding="utf-8", errors="replace"), str(p)
    return text_or_path, "(inline text)"


def _chat(
    messages: List[Dict[str, str]], model: str, max_tokens: int = 4096
) -> Dict[str, Any]:
    url = PROXY_BASE + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    with httpx.Client(timeout=300) as c:
        r = c.post(url, json=payload)
    if r.status_code != 200:
        raise RuntimeError("代理返回 %s：%s" % (r.status_code, r.text[:300]))
    return r.json()


# ---------------------------------------------------------------- MCP 工具


@mcp.tool()
def lint_prompt(text_or_path: str) -> Dict[str, Any]:
    """零成本规则检查任务卡/提示词：模糊词、缺禁止项、缺验收标准。

    text_or_path 可以是正文，也可以是文档路径（如 docs/bench/UI_BATCH_A.md）。
    """
    text, src = _read_input(text_or_path)
    result = _lint(text)
    result["source"] = src
    return result


@mcp.tool()
def review_criteria(text_or_path: str, model: Optional[str] = None) -> Dict[str, Any]:
    """用模型做「歧义分析」：找出任务卡里会导致理解分歧的表述，并给改写建议。

    会通过本地代理调用（计入账本），默认 deepseek-flash 省钱；
    需要更强判断力时可传 model="gemini-4-argon"。
    """
    text, src = _read_input(text_or_path)
    truncated = False
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        truncated = True

    use = model or DEFAULT_MODEL
    try:
        data = _chat(
            [
                {"role": "system", "content": CRITERIA_PROMPT},
                {"role": "user", "content": text},
            ],
            use,
        )
    except Exception as exc:  # 代理没起 / 限流 / 上游报错
        return {
            "ok": False,
            "model": use,
            "error": str(exc)[:400],
            "hint": "确认代理在 %s 上运行，或换一个模型" % PROXY_BASE,
        }

    msg = (data.get("choices") or [{}])[0].get("message") or {}
    usage = data.get("usage") or {}
    return {
        "ok": True,
        "model": data.get("model") or use,
        "source": src,
        "truncated": truncated,
        "analysis": (msg.get("content") or "").strip(),
        "usage": usage,
    }


@mcp.tool()
def get_usage() -> Dict[str, Any]:
    """查今日花费与限额水位。跑重活前先看一眼，避免撞限额。"""
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(PROXY_BASE + "/stats?days=1")
        if r.status_code != 200:
            return {"ok": False, "error": "代理返回 %s" % r.status_code}
        t = (r.json() or {}).get("today") or {}
        lim = t.get("limits") or {}
        cost = float(t.get("cost") or 0)
        cap = float(lim.get("daily_cost") or 0)
        return {
            "ok": True,
            "date": t.get("date"),
            "requests": t.get("requests"),
            "tokens_total": t.get("tokens_total"),
            "cache_hit_ratio": t.get("cache_hit_ratio"),
            "cost": round(cost, 4),
            "cost_limit": cap,
            "cost_pct": round(cost / cap * 100, 1) if cap else None,
            "uncached": t.get("tokens_uncached"),
            "uncached_limit": lim.get("daily_uncached"),
            "by_model": t.get("by_model") or {},
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def main() -> None:
    # stdio 模式下 stdout 是协议通道，任何调试输出必须走 stderr
    print(
        "[criteria-mcp] proxy=%s model=%s" % (PROXY_BASE, DEFAULT_MODEL),
        file=sys.stderr,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
