# -*- coding: utf-8 -*-
"""
claude_adapter.py
=================
把 OpenAI 格式的对话/工具定义转换成 Anthropic (Vertex) 格式，并把返回转回 OpenAI 结构。

只在 model 以 claude- 开头时使用。凭证走与 Gemini 相同的服务账号 JSON。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

from anthropic import AnthropicVertex

DEFAULT_REGION = "global"  # 实测 Claude 只在 global 上架（us-east5 等均 404）
MAX_TOKENS_DEFAULT = 8192

_client: Optional[AnthropicVertex] = None
_client_sig: Optional[str] = None


class ClaudeConfigError(RuntimeError):
    pass


def _sa_path() -> str:
    return (
        os.getenv("VERTEX_SA_JSON") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or ""
    ).strip()


def get_client() -> AnthropicVertex:
    """惰性初始化 AnthropicVertex（服务账号鉴权，带签名缓存）。"""
    global _client, _client_sig

    project = (
        os.getenv("CLAUDE_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or ""
    ).strip()
    region = (os.getenv("CLAUDE_REGION") or DEFAULT_REGION).strip()
    sa = _sa_path()

    if not project:
        raise ClaudeConfigError("Claude 需要 GOOGLE_CLOUD_PROJECT（或 CLAUDE_PROJECT）")
    if not sa or not os.path.exists(sa):
        raise ClaudeConfigError(
            "Claude 需要服务账号 JSON：请设置 VERTEX_SA_JSON 指向密钥文件"
        )

    sig = "|".join([project, region, sa])
    if _client is not None and _client_sig == sig:
        return _client

    # anthropic SDK 走 httpx，需要显式代理
    proxy = (os.getenv("VERTEX_PROXY") or os.getenv("HTTPS_PROXY") or "").strip()
    if proxy:
        os.environ.setdefault("HTTPS_PROXY", proxy)
        os.environ.setdefault("HTTP_PROXY", proxy)

    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        sa, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    _client = AnthropicVertex(project_id=project, region=region, credentials=creds)
    _client_sig = sig
    return _client


# --------------------------------------------------------------------------
# OpenAI -> Anthropic
# --------------------------------------------------------------------------
def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def _image_block(url: str) -> Optional[Dict[str, Any]]:
    if url.startswith("data:") and ";base64," in url:
        head, b64 = url.split(";base64,", 1)
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": head[5:] or "image/png",
                "data": b64,
            },
        }
    return None


def _content_blocks(content: Any) -> List[Dict[str, Any]]:
    """OpenAI content -> Anthropic content blocks（text / image）。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                if item:
                    blocks.append({"type": "text", "text": item})
            elif isinstance(item, dict):
                t = (item.get("type") or "").lower()
                if t in ("text", "input_text"):
                    if item.get("text"):
                        blocks.append({"type": "text", "text": item["text"]})
                elif t == "image_url":
                    url = (item.get("image_url") or {}).get("url") or ""
                    blk = _image_block(url)
                    if blk:
                        blocks.append(blk)
    return blocks or [{"type": "text", "text": ""}]


def build_messages(messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """返回 (system, anthropic_messages)，并处理 tool_calls / tool 结果。"""
    system_chunks: List[str] = []
    out: List[Dict[str, Any]] = []

    def push(role: str, blocks: List[Dict[str, Any]]) -> None:
        if not blocks:
            return
        # 含 tool_result 的 turn 不能并入正文：Anthropic 要求 tool_result 在 content 最前，
        # 混进后续文本虽然多数情况能过，但容易触发校验错误，这里一律单独成 turn
        last_is_tool_result = (
            bool(out)
            and out[-1]["role"] == role
            and any(b.get("type") == "tool_result" for b in out[-1]["content"])
        )
        if out and out[-1]["role"] == role and not last_is_tool_result:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for msg in messages or []:
        role = (msg.get("role") or "user").lower()

        if role in ("system", "developer"):
            text = _text_of(msg.get("content"))
            if text.strip():
                system_chunks.append(text)
            continue

        if role == "tool":
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(msg.get("tool_call_id") or ""),
                        "content": _text_of(msg.get("content")),
                    }
                ],
            )
            continue

        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            text = _text_of(msg.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if not fn.get("name"):
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tc.get("id") or ""),
                        "name": fn["name"],
                        "input": args if isinstance(args, dict) else {},
                    }
                )
            push("assistant", blocks)
            continue

        push("user", _content_blocks(msg.get("content")))

    # Anthropic 要求首条是 user
    if out and out[0]["role"] != "user":
        out.insert(
            0, {"role": "user", "content": [{"type": "text", "text": "Continue."}]}
        )

    return "\n\n".join(system_chunks), out


def build_tools(body: Dict[str, Any]) -> Tuple[Optional[List[Dict[str, Any]]], Any]:
    """OpenAI tools -> Anthropic tools；tool_choice 一并映射。"""
    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict) or t.get("type") not in (None, "function"):
            continue
        fn = t.get("function") or {}
        if not fn.get("name"):
            continue
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        # Anthropic 不接受 $schema / additionalProperties 之类的顶层关键字
        schema = {
            k: v
            for k, v in schema.items()
            if k not in ("$schema", "$id", "$comment", "strict")
        }
        schema.setdefault("type", "object")
        tools.append(
            {
                "name": fn["name"],
                "description": fn.get("description") or "",
                "input_schema": schema,
            }
        )

    choice = body.get("tool_choice")
    tc: Any = None
    if choice == "none":
        tc = {"type": "none"}
    elif choice == "required":
        tc = {"type": "any"}
    elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
        tc = {"type": "tool", "name": choice["function"]["name"]}

    return (tools or None), tc


def build_kwargs(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    system, messages = build_messages(body.get("messages") or [])
    tools, tool_choice = build_tools(body)

    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": int(
            body.get("max_tokens")
            or body.get("max_completion_tokens")
            or MAX_TOKENS_DEFAULT
        ),
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if body.get("temperature") is not None:
        kwargs["temperature"] = float(body["temperature"])
    if body.get("top_p") is not None:
        kwargs["top_p"] = float(body["top_p"])
    stop = body.get("stop")
    if isinstance(stop, str):
        kwargs["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        kwargs["stop_sequences"] = [str(s) for s in stop]
    if tools:
        kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
    return kwargs


# --------------------------------------------------------------------------
# Anthropic -> OpenAI
# --------------------------------------------------------------------------
_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def normalize_stop(reason: Optional[str], has_tools: bool) -> str:
    if has_tools:
        return "tool_calls"
    return _FINISH.get(reason or "", "stop")


def to_openai(
    resp: Any, model: str, completion_id: str, created: int
) -> Dict[str, Any]:
    text_parts: List[str] = []
    calls: List[Dict[str, Any]] = []
    for block in getattr(resp, "content", None) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "tool_use":
            calls.append(
                {
                    "id": getattr(block, "id", "") or "call_" + str(len(calls)),
                    "type": "function",
                    "function": {
                        "name": getattr(block, "name", ""),
                        "arguments": json.dumps(
                            getattr(block, "input", {}) or {}, ensure_ascii=False
                        ),
                    },
                }
            )

    text = "".join(text_parts)
    message: Dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = calls

    usage = getattr(resp, "usage", None)
    prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": normalize_stop(
                    getattr(resp, "stop_reason", None), bool(calls)
                ),
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def create(client: Any, kwargs: Dict[str, Any]) -> Any:
    return client.messages.create(**kwargs)


def iter_stream(client: Any, kwargs: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """流式：先吐文本增量，最后把 tool_use 作为一批 tool_calls 吐出。"""
    with client.messages.stream(**kwargs) as stream:
        for delta in stream.text_stream:
            if delta:
                yield {"type": "text", "text": delta}
        final = stream.get_final_message()

    calls: List[Dict[str, Any]] = []
    for block in getattr(final, "content", None) or []:
        if getattr(block, "type", "") == "tool_use":
            calls.append(
                {
                    "id": getattr(block, "id", "") or "call_0",
                    "type": "function",
                    "function": {
                        "name": getattr(block, "name", ""),
                        "arguments": json.dumps(
                            getattr(block, "input", {}) or {}, ensure_ascii=False
                        ),
                    },
                }
            )

    if calls:
        yield {"type": "tool_calls", "calls": calls}

    usage = getattr(final, "usage", None)
    yield {
        "type": "finish",
        "reason": normalize_stop(getattr(final, "stop_reason", None), bool(calls)),
        "usage": {
            "in": int(getattr(usage, "input_tokens", 0) or 0),
            "out": int(getattr(usage, "output_tokens", 0) or 0),
        },
    }
