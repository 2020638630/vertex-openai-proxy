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


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)) or default)
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


def _read_price_file_raw() -> Dict[str, Any]:
    try:
        with open(_price_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def cache_discount() -> float:
    """缓存命中的折扣（0.9 = 打一折）。官方隐式缓存默认 90% off。"""
    try:
        return float(_read_price_file_raw().get("_cache_discount", 0.9))
    except (TypeError, ValueError):
        return 0.9


def _fx_rates(raw: Dict[str, Any]) -> Dict[str, float]:
    """读取 _fx 汇率表，统一折算成 USD（各家官网有的用人民币报价）。"""
    rates = {"USD": 1.0}
    for cur, val in (raw.get("_fx") or {}).items():
        try:
            rates[str(cur).upper()] = float(val)
        except (TypeError, ValueError):
            continue
    return rates


def load_prices() -> Dict[str, Dict[str, float]]:
    raw = _read_price_file_raw()
    fx = _fx_rates(raw)
    out: Dict[str, Dict[str, float]] = {}
    for model, val in raw.items():
        if model.startswith("_") or not isinstance(val, dict):
            continue
        try:
            pin = float(val["in"])
            pout = float(val["out"])
        except (KeyError, TypeError, ValueError):
            continue
        rate = fx.get(str(val.get("currency") or "USD").upper(), 1.0) or 1.0
        entry = {"in": pin / rate, "out": pout / rate}
        # 可选：缓存命中单价。各家折扣不同（Gemini 约 1 折，DeepSeek 约 2%~3%），
        # 没配就退回 in × (1 - _cache_discount)
        try:
            if val.get("cached_in") is not None:
                entry["cached_in"] = float(val["cached_in"]) / rate
        except (TypeError, ValueError):
            pass
        out[model] = entry
    return out


def prices_note() -> str:
    return str(_read_price_file_raw().get("_note") or "")


def cost_of(
    prices: Dict[str, Dict[str, float]],
    model: str,
    tin: int,
    tout: int,
    cached: int = 0,
    discount: float = 0.9,
) -> Optional[float]:
    """估算花费。cached 为命中缓存的输入 token（按 discount 打折）。"""
    if model.endswith(":free"):  # OpenRouter 的免费模型，无需在价格表里逐个登记
        return 0.0
    p = prices.get(model)
    if not p:
        return None
    cached = max(0, min(int(cached or 0), int(tin or 0)))
    uncached = max(0, int(tin or 0) - cached)
    cached_unit = p.get("cached_in", p["in"] * (1 - discount))
    return (
        uncached / 1e6 * p["in"]
        + cached / 1e6 * cached_unit
        + int(tout or 0) / 1e6 * p["out"]
    )


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
        self.daily_tokens = _int_env("DAILY_TOKEN_LIMIT", 10_000_000)
        # 主口径：只算「未命中缓存」的输入 token（命中部分官方打一折）
        self.daily_uncached = _int_env("DAILY_UNCACHED_LIMIT", 5_000_000)
        # 兜底：总量超过限额这么多倍时，即使当前会话也强制拦（0 = 关闭）
        self.hard_mult = _float_env("LIMIT_HARD_MULTIPLIER", 3.0)
        self.keep_days = _int_env("USAGE_KEEP_DAYS", 14)
        self._conv_fp = ""
        self._lock = threading.Lock()
        self._loaded_day = ""
        self._last_cleanup = 0.0
        self._last_alive = 0.0
        self._reset()

    # ---------------------------------------------------------------- 内部
    def _reset(self) -> None:
        self.requests = 0
        self.fail = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.tokens_cached = 0
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

    def _alive_path(self, day: Optional[str] = None) -> str:
        return os.path.join(self.dir, "%s.alive" % (day or _today()))

    def touch_alive(self, force: bool = False) -> None:
        """写当日心跳文件：用于区分「代理没运行」和「运行了但 0 调用」。

        节流到每分钟一次，避免 /healthz 被打爆时频繁落盘。
        """
        now = time.time()
        if not force and now - self._last_alive < 60:
            return
        self._last_alive = now
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self._alive_path(), "w", encoding="utf-8") as f:
                f.write(str(int(now)))
        except Exception as exc:  # 心跳失败不能影响服务
            log.warning("写心跳失败: %s", exc)

    def _apply(self, rec: Dict[str, Any]) -> None:
        self.requests += 1
        if not rec.get("ok", True):
            self.fail += 1
        self.tokens_in += int(rec.get("in") or 0)
        self.tokens_out += int(rec.get("out") or 0)
        self.tokens_cached += int(rec.get("cache") or 0)
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
        cached: int = 0,
    ) -> None:
        rec = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "model": model,
            "provider": provider,
            "in": int(in_tokens or 0),
            "out": int(out_tokens or 0),
            "th": int(thoughts or 0),
            "cache": int(cached or 0),
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

    def session_start(self, fp: str) -> bool:
        """判断这是不是一段新会话（按首条用户消息的指纹）。"""
        with self._lock:
            new = fp != self._conv_fp
            self._conv_fp = fp
            return new

    def check(self, new_session: bool = False) -> List[str]:
        """请求入口调用。

        口径：主看「未命中缓存」的输入 token（命中部分官方打一折），
        另有一条总 token 兜底。

        软着陆：额度用完后**不打断当前会话**，只记预警；
        等到下一段会话开始（new_session=True）才真正返回 429。
        例外：总量超过 LIMIT_HARD_MULTIPLIER 倍时强制拦截（防死循环），
        该倍率设为 0 可完全关闭。
        """
        with self._lock:
            self._ensure_day()
            now = time.time()
            while self.window and now - self.window[0] > 60:
                self.window.popleft()
            self.window.append(now)

            problems: List[str] = []
            retry = 0
            rpm_hit = bool(self.rpm and len(self.window) > self.rpm)
            if rpm_hit:
                problems.append(
                    "近 60 秒请求数 %d，超过上限 %d" % (len(self.window), self.rpm)
                )
            if self.daily_req and self.requests >= self.daily_req:
                problems.append(
                    "今日请求 %d 次，超过上限 %d" % (self.requests, self.daily_req)
                )
                retry = max(retry, _seconds_to_midnight())

            total = self.tokens_in + self.tokens_out
            uncached = max(0, self.tokens_in - self.tokens_cached)
            if self.daily_uncached and uncached >= self.daily_uncached:
                problems.append(
                    "今日未命中缓存的输入 %d tokens，超过上限 %d"
                    % (uncached, self.daily_uncached)
                )
                retry = max(retry, _seconds_to_midnight())
            if self.daily_tokens and total >= self.daily_tokens:
                problems.append(
                    "今日总用量 %d tokens，超过上限 %d" % (total, self.daily_tokens)
                )
                retry = max(retry, _seconds_to_midnight())

            hard = bool(
                self.hard_mult
                and self.daily_tokens
                and total >= self.daily_tokens * self.hard_mult
            )

        if not problems:
            return []

        text = "；".join(problems)
        if self.mode != "block":
            log.warning("[额度预警] %s（LIMIT_MODE=warn，未拦截）", text)
            return problems

        # 频率超限属于即时保护，直接拦（60 秒后可重试），不参与软着陆
        if rpm_hit:
            raise BudgetExceeded(text, 60)

        if hard:
            log.warning(
                "[额度熔断] %s（已超 %.0f 倍上限，强制拦截）", text, self.hard_mult
            )
            raise BudgetExceeded(text + "；已超硬阈值，强制拦截", retry or 60)

        if new_session:
            log.warning("[额度拦截] %s（新会话开始，拦截）", text)
            raise BudgetExceeded(
                text + "；上一段会话已结束，请明天再试或调高限额", retry or 60
            )

        # 当前会话中途：放过，只在日志里预警，等下一段会话再拦
        log.warning("[额度预警] %s（当前会话继续放行，将在下一会话拦截）", text)
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
                "tokens_cached": self.tokens_cached,
                "tokens_uncached": max(0, self.tokens_in - self.tokens_cached),
                "tokens_total": self.tokens_in + self.tokens_out,
                "by_model": dict(self.by_model),
                "limits": {
                    "rpm": self.rpm,
                    "daily_requests": self.daily_req,
                    "daily_tokens": self.daily_tokens,
                    "daily_uncached": self.daily_uncached,
                    "hard_multiplier": self.hard_mult,
                },
            }

    def stats(self, days: int = 7) -> Dict[str, Any]:
        today = date.today()
        prices = load_prices()
        discount = cache_discount()
        out: Dict[str, Any] = {
            "today": {},
            "daily": [],
            "errors": [],
            "prices_note": prices_note(),
        }
        missing: set = set()
        total = {"requests": 0, "tokens": 0, "fail": 0, "cost": 0.0, "cached": 0}

        for i in range(max(1, days)):
            d = today - timedelta(days=i)
            path = self._path(d.isoformat())
            row: Dict[str, Any] = {
                "date": d.isoformat(),
                "requests": 0,
                "tokens": 0,
                "fail": 0,
                "cost": 0.0,
                "cached": 0,
                # 心跳：区分「那天代理没运行」和「运行了但 0 调用」
                "alive": os.path.exists(self._alive_path(d.isoformat())),
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
                            hit = int(rec.get("cache") or 0)
                            row["cached"] = row.get("cached", 0) + hit
                            c = cost_of(prices, model, tin, tout, hit, discount)
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
                snap["cached"] = row["cached"]
                tin = int(snap.get("tokens_in") or 0)
                snap["cache_hit_ratio"] = round(row["cached"] / tin, 4) if tin else 0.0
                snap["alive"] = row["alive"]
                out["today"] = snap
            for k in ("requests", "tokens", "fail", "cached"):
                total[k] += row[k]
            total["cost"] += row["cost"]
            out["daily"].append(row)

        total["cost"] = round(total["cost"], 4)
        out["total"] = total
        out["missing_prices"] = sorted(missing)
        return out
