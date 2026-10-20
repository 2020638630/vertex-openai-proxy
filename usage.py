# -*- coding: utf-8 -*-
"""
usage.py
========
用量记账与限额守卫。

* 记账：每天一个 JSONL 文件（data/usage/YYYY-MM-DD.jsonl），每请求一行，append-only
* 计数：启动时读回当天文件，重启不清零；跨天自动重置
* 限额：RPM（60s 滑动窗口）/ 日请求数 / 日 token
* 模式：LIMIT_MODE=warn 只告警（默认），=block 才真正返回 429
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter, deque
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

log = logging.getLogger("usage")

DEFAULT_DIR = os.getenv("USAGE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "usage"
)


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)) or default)
    except ValueError:
        return default


class BudgetExceeded(RuntimeError):
    """block 模式下超限抛出。"""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _today() -> str:
    return date.today().isoformat()


# --------------------------------------------------------------------------
# 价格表（可选，只用于估算花费，不参与拦截）
# --------------------------------------------------------------------------
def _price_path() -> str:
    return os.getenv("PRICES_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "prices.json"
    )


def load_prices() -> Dict[str, Dict[str, float]]:
    try:
        with open(_price_path(), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for model, val in raw.items():
        if model.startswith("_") or not isinstance(val, dict):
            continue
        try:
            pin = float(val["in"])
            pout = float(val["out"])
        except (KeyError, TypeError, ValueError):
            continue
        out[model] = {"in": pin, "out": pout}
    return out


def prices_note() -> str:
    try:
        with open(_price_path(), encoding="utf-8") as f:
            return str((json.load(f) or {}).get("_note") or "")
    except (OSError, ValueError):
        return ""


def cost_of(
    prices: Dict[str, Dict[str, float]], model: str, tin: int, tout: int
) -> Optional[float]:
    p = prices.get(model)
    if not p:
        return None
    return tin / 1e6 * p["in"] + tout / 1e6 * p["out"]


def _seconds_to_midnight() -> int:
    now = datetime.now()
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    return max(1, int((tomorrow - now).total_seconds()))


class Usage:
    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.dir = data_dir or DEFAULT_DIR
        self.mode = (os.getenv("LIMIT_MODE") or "warn").strip().lower()
        self.rpm = _int_env("RPM_LIMIT", 30)
        self.daily_req = _int_env("DAILY_REQ_LIMIT", 500)
        self.daily_tokens = _int_env("DAILY_TOKEN_LIMIT", 2_000_000)
        self.keep_days = _int_env("USAGE_KEEP_DAYS", 14)
        self._lock = threading.Lock()
        self._loaded_day = ""
        self._last_cleanup = 0.0
        self._reset()

    # ---------------------------------------------------------------- 内部
    def _reset(self) -> None:
        self.requests = 0
        self.fail = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.by_model: Counter = Counter()
        self.window: deque = deque()

    def _path(self, day: Optional[str] = None) -> str:
        return os.path.join(self.dir, "%s.jsonl" % (day or _today()))

    def _ensure_day(self) -> None:
        day = _today()
        if day == self._loaded_day:
            return
        self._reset()
        self._loaded_day = day
        path = self._path(day)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        self._apply(json.loads(line))
            except Exception as exc:  # 账本坏了不能影响服务
                log.warning("读取当日用量失败: %s", exc)

    def _apply(self, rec: Dict[str, Any]) -> None:
        self.requests += 1
        if not rec.get("ok", True):
            self.fail += 1
        self.tokens_in += int(rec.get("in") or 0)
        self.tokens_out += int(rec.get("out") or 0)
        self.by_model[str(rec.get("model") or "?")] += 1

    def _cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        cutoff = date.today() - timedelta(days=max(1, self.keep_days))
        try:
            for name in os.listdir(self.dir):
                if not name.endswith(".jsonl"):
                    continue
                try:
                    d = date.fromisoformat(name[:10])
                except ValueError:
                    continue
                if d < cutoff:
                    os.remove(os.path.join(self.dir, name))
        except OSError:
            pass

    # ---------------------------------------------------------------- 对外
    def record(
        self,
        model: str,
        in_tokens: int = 0,
        out_tokens: int = 0,
        ms: int = 0,
        ok: bool = True,
        tools: int = 0,
        provider: str = "gemini",
        error: str = "",
        thoughts: int = 0,
    ) -> None:
        rec = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "model": model,
            "provider": provider,
            "in": int(in_tokens or 0),
            "out": int(out_tokens or 0),
            "th": int(thoughts or 0),
            "ms": int(ms or 0),
            "ok": bool(ok),
            "tools": int(tools or 0),
        }
        if error:
            rec["err"] = error[:160]
        with self._lock:
            self._ensure_day()
            self._apply(rec)
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(self._path(), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError as exc:
                log.warning("写入用量失败: %s", exc)
        self._cleanup()

    def check(self) -> List[str]:
        """请求入口调用；返回告警文案列表。block 模式下超限抛 BudgetExceeded。"""
        with self._lock:
            self._ensure_day()
            now = time.time()
            while self.window and now - self.window[0] > 60:
                self.window.popleft()
            self.window.append(now)

            problems: List[str] = []
            retry = 0
            if self.rpm and len(self.window) > self.rpm:
                problems.append(
                    "近 60 秒请求数 %d，超过上限 %d" % (len(self.window), self.rpm)
                )
                retry = 60
            if self.daily_req and self.requests >= self.daily_req:
                problems.append(
                    "今日请求 %d 次，超过上限 %d" % (self.requests, self.daily_req)
                )
                retry = max(retry, _seconds_to_midnight())
            used = self.tokens_in + self.tokens_out
            if self.daily_tokens and used >= self.daily_tokens:
                problems.append(
                    "今日已用 %d tokens，超过上限 %d" % (used, self.daily_tokens)
                )
                retry = max(retry, _seconds_to_midnight())

        if not problems:
            return []
        text = "；".join(problems)
        if self.mode == "block":
            raise BudgetExceeded(text, retry or 60)
        # warn 模式：只在日志里说一声，不打扰客户端
        log.warning("[额度预警] %s（当前 LIMIT_MODE=warn，未拦截）", text)
        return problems

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._ensure_day()
            return {
                "date": self._loaded_day,
                "mode": self.mode,
                "requests": self.requests,
                "fail": self.fail,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "tokens_total": self.tokens_in + self.tokens_out,
                "by_model": dict(self.by_model),
                "limits": {
                    "rpm": self.rpm,
                    "daily_requests": self.daily_req,
                    "daily_tokens": self.daily_tokens,
                },
            }

    def stats(self, days: int = 7) -> Dict[str, Any]:
        today = date.today()
        prices = load_prices()
        out: Dict[str, Any] = {
            "today": {},
            "daily": [],
            "errors": [],
            "prices_note": prices_note(),
        }
        missing: set = set()
        total = {"requests": 0, "tokens": 0, "fail": 0, "cost": 0.0}

        for i in range(max(1, days)):
            d = today - timedelta(days=i)
            path = self._path(d.isoformat())
            row: Dict[str, Any] = {
                "date": d.isoformat(),
                "requests": 0,
                "tokens": 0,
                "fail": 0,
                "cost": 0.0,
            }
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            rec = json.loads(line)
                            tin = int(rec.get("in") or 0)
                            tout = int(rec.get("out") or 0)
                            row["requests"] += 1
                            row["tokens"] += tin + tout
                            if not rec.get("ok", True):
                                row["fail"] += 1
                                if len(out["errors"]) < 10:
                                    out["errors"].append(
                                        {
                                            "date": d.isoformat(),
                                            "ts": rec.get("ts"),
                                            "model": rec.get("model"),
                                            "err": rec.get("err", ""),
                                        }
                                    )
                            model = str(rec.get("model") or "")
                            c = cost_of(prices, model, tin, tout)
                            if c is None:
                                if model:
                                    missing.add(model)
                            else:
                                row["cost"] += c
                except Exception:
                    pass

            row["cost"] = round(row["cost"], 4)
            if i == 0:
                snap = self.snapshot()
                snap["cost"] = row["cost"]
                out["today"] = snap
            for k in ("requests", "tokens", "fail"):
                total[k] += row[k]
            total["cost"] += row["cost"]
            out["daily"].append(row)

        total["cost"] = round(total["cost"], 4)
        out["total"] = total
        out["missing_prices"] = sorted(missing)
        return out
