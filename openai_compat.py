# -*- coding: utf-8 -*-
"""
openai_compat.py
================
通用 OpenAI 兼容通道：一个实现覆盖

    DeepSeek / 智谱 GLM / xAI Grok / OpenAI / Ollama / 自建中转站

只做三件事：路由、转发、把 usage 字段归一（各家命名不同）。
不重写协议 —— 上游本来就吐 OpenAI 格式，原样透传，只在文本上做脱敏还原。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

log = logging.getLogger("openai-compat")

CONFIG_FILE = os.getenv("PROVIDERS_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "providers.json"
)

# 透传给上游的字段白名单（其余字段丢弃，避免上游报 unknown field）
_PASS_FIELDS = (
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "parallel_tool_calls",
    "user",
)

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


# --------------------------------------------------------------------------
# 配置与路由
# --------------------------------------------------------------------------
def load_providers(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    p = path or CONFIG_FILE
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f) or {}
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, cfg in raw.items():
        if name.startswith("_") or not isinstance(cfg, dict):
            continue
        out[name] = cfg
    return out


def _usable(cfg: Dict[str, Any]) -> bool:
    return bool((cfg.get("base_url") or "").strip())


def resolve(
    model: str, providers: Optional[Dict[str, Dict[str, Any]]] = None
) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """混合路由：先看 `provider/model` 前缀，再查各 provider 的 models 列表。

    返回 (provider_name, upstream_model, cfg)；不匹配返回 None（交给 Gemini 通道）。
    """
    model = (model or "").strip()
    if not model:
        return None
    providers = load_providers() if providers is None else providers
    if not providers:
        return None

    if "/" in model:
        name, rest = model.split("/", 1)
        cfg = providers.get(name)
        if cfg is not None and rest:
            if _usable(cfg):
                return name, rest, cfg
            log.warning("provider %s 未配置 base_url，跳过", name)
            return None

    for name, cfg in providers.items():
        if not _usable(cfg):
            continue
        if model in (cfg.get("models") or []):
            return name, model, cfg
    return None


# --------------------------------------------------------------------------
# 请求构造
# --------------------------------------------------------------------------
def _api_key(cfg: Dict[str, Any]) -> str:
    env_name = (cfg.get("api_key_env") or "").strip()
    if env_name:
        key = (os.getenv(env_name) or "").strip()
        if key:
            return key
    return (cfg.get("api_key") or "").strip()


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    key = _api_key(cfg)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    for k, v in (cfg.get("headers") or {}).items():
        headers[str(k)] = str(v).replace("{key}", key)
    return headers


def build_payload(body: Dict[str, Any], upstream_model: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in _PASS_FIELDS:
        if body.get(key) is not None:
            payload[key] = body[key]
    payload["model"] = upstream_model

    # 有些客户端同时发 max_tokens 和 max_completion_tokens，Gemini 等上游会直接 400
    # （"max_tokens and max_completion_tokens cannot both be set"）。统一只保留一个。
    if payload.get("max_completion_tokens") is not None:
        if payload.get("max_tokens") is None:
            payload["max_tokens"] = payload["max_completion_tokens"]
        payload.pop("max_completion_tokens", None)

    if body.get("stream"):
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return payload


def map_usage(usage: Any) -> Dict[str, int]:
    """各家 usage 字段归一：OpenAI / DeepSeek / Anthropic 风格都认。"""
    if not isinstance(usage, dict):
        return {"in": 0, "out": 0, "cached": 0}
    tin = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    tout = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    cached = 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or 0
    if not cached:  # DeepSeek 风格
        cached = usage.get("prompt_cache_hit_tokens") or 0
    if not cached:  # Anthropic 风格
        cached = usage.get("cache_read_input_tokens") or 0
    return {"in": int(tin), "out": int(tout), "cached": int(cached)}


def _url(cfg: Dict[str, Any]) -> str:
    return (cfg.get("base_url") or "").rstrip("/") + "/chat/completions"


def _proxy_for(url: str) -> Optional[str]:
    """本地服务（Ollama）不走代理；外网走 .env 里的 VERTEX_PROXY。"""
    host = (urlparse(url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return None
    return (os.getenv("VERTEX_PROXY") or os.getenv("HTTPS_PROXY") or "").strip() or None


def _timeout() -> float:
    try:
        return float(os.getenv("COMPAT_TIMEOUT", "600") or 600)
    except ValueError:
        return 600.0


def _client(url: str) -> httpx.Client:
    proxy = _proxy_for(url)
    kwargs: Dict[str, Any] = {"timeout": _timeout()}
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------
def _check(r: Any) -> None:
    """把上游错误体带出来：默认的 raise_for_status 只有状态码，排查时看不到原因。"""
    if r.status_code < 400:
        return
    detail = ""
    try:
        # 流式响应必须先 read()，直接取 .text 会是空串
        raw = r.read()
        detail = (
            raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        )[:500]
    except Exception:
        try:
            detail = r.text[:500]
        except Exception:
            detail = "(无法读取响应体)"
    raise httpx.HTTPStatusError(
        "HTTP %d: %s" % (r.status_code, detail), request=r.request, response=r
    )


def post_json(cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    url = _url(cfg)
    with _client(url) as c:
        r = c.post(url, json=payload, headers=_headers(cfg))
    _check(r)
    return r.json()


def iter_chunks(
    cfg: Dict[str, Any], payload: Dict[str, Any]
) -> Iterator[Dict[str, Any]]:
    """逐条吐出上游的 SSE 数据（已解析成 dict），遇到 [DONE] 结束。"""
    url = _url(cfg)
    with _client(url) as c:
        with c.stream("POST", url, json=payload, headers=_headers(cfg)) as r:
            _check(r)
            for line in r.iter_lines():
                line = (line or "").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except ValueError:
                    continue
