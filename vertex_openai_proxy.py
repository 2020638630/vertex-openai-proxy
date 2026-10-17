# -*- coding: utf-8 -*-
"""
vertex_openai_proxy.py
======================
本地 OpenAI 兼容网关：把标准 OpenAI /v1/chat/completions 请求
通过官方 SDK (google-genai) 的 Vertex AI Express 模式
    genai.Client(vertexai=True, api_key="AIza...")
转发到 Google Cloud Vertex AI。

启动:  python vertex_openai_proxy.py      (默认 http://127.0.0.1:4000)
文档:  http://127.0.0.1:4000/docs
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import random
import sys
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# 必须早于读取环境变量，且 override=True 让 .env 覆盖 shell 里的空值
load_dotenv(override=True)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict

from sanitizer import Sanitizer, StreamRestorer

# 日志固定 UTF-8 输出，避免 Windows 下重定向到文件时按 GBK 写坏中文
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vertex-proxy")

HOST = os.getenv("PROXY_HOST", "127.0.0.1")
PORT = int(os.getenv("PROXY_PORT", "4000") or 4000)
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gemini-2.5-pro")

# 客户端下拉列表里能看到的模型（已实测当前 Key + project/location=global 可用）
MODELS = [
    "gemini-4-argon",
    "gemini-3.8-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

# 常见但当前项目不可用的名字 -> 自动改写到可用模型，避免客户端硬编码 404
MODEL_ALIASES = {
    "gemini-2.0-flash": "gemini-2.5-flash",
    "gemini-2.0-flash-lite": "gemini-2.5-flash-lite",
    "gemini-2.0-flash-001": "gemini-2.5-flash",
    "gemini-1.5-pro": "gemini-2.5-pro",
    "gemini-1.5-flash": "gemini-2.5-flash",
    "gemini-pro": "gemini-2.5-pro",
    "gemini-pro-latest": "gemini-2.5-pro",
    "gemini-flash-latest": "gemini-2.5-flash",
}

# --------------------------------------------------------------------------
# thought_signature 缓存
# 3.8-flash / 4-argon 要求回传的 functionCall 必须带原始 thought_signature，
# 但 OpenAI 协议里没有这个字段，客户端回传历史时必然丢失。
# 这里在网关内按 tool_call_id 记住原始 part，回传时原样塞回去。
# 注意：进程重启后缓存丢失，历史里带旧 tool_call 的会话会 400，重新开对话即可。
# --------------------------------------------------------------------------
_SIG_CACHE: "OrderedDict[str, Any]" = OrderedDict()
_SIG_CACHE_MAX = 800


def _remember_part(call_id: str, part: Any) -> None:
    if not call_id or part is None:
        return
    _SIG_CACHE[call_id] = part
    _SIG_CACHE.move_to_end(call_id)
    while len(_SIG_CACHE) > _SIG_CACHE_MAX:
        _SIG_CACHE.popitem(last=False)


app = FastAPI(title="Vertex AI OpenAI-Compatible Proxy", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# 客户端（全局单例）
# --------------------------------------------------------------------------
_client: Optional[genai.Client] = None
_client_sig: Optional[str] = None


def _auth_mode() -> Tuple[str, str, str, str]:
    """解析鉴权方式，返回 (mode, key, project, location)。

    mode = "service_account" 走标准 Vertex（项目配额，TPM 高）
         = "api_key"         走 Express（共享池，配额小、易 429）
    VERTEX_AUTH=auto 时：配置了服务账号就优先用它。
    """
    auth = (os.getenv("VERTEX_AUTH") or "auto").strip().lower()
    key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "").strip()
    sa_path = (os.getenv("VERTEX_SA_JSON") or "").strip()
    adc = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()

    has_sa = bool((sa_path and os.path.exists(sa_path)) or (adc and os.path.exists(adc)))
    if auth == "service_account":
        mode = "service_account"
    elif auth == "api_key":
        mode = "api_key"
    else:
        mode = "service_account" if (has_sa and project and location) else "api_key"
    return mode, key, project, location


def get_client() -> genai.Client:
    """惰性初始化：即使 .env 还没填 Key，服务也能起来并给出可读报错。"""
    global _client, _client_sig

    # 每次请求前重读 .env：填 Key / 换 Key 后都无需重启服务
    load_dotenv(override=True)
    mode, key, project, location = _auth_mode()

    if mode == "service_account":
        sa_path = (os.getenv("VERTEX_SA_JSON") or "").strip()
        if sa_path and os.path.exists(sa_path):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path
        sig = "sa|%s|%s|%s" % (project, location, os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
    else:
        sig = "key|%s|%s|%s" % (key, project, location)

    if _client is not None and _client_sig == sig:
        return _client

    if mode == "service_account":
        if not project or not location:
            raise RuntimeError(
                "服务账号模式需要 GOOGLE_CLOUD_PROJECT 与 GOOGLE_CLOUD_LOCATION"
            )
        if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            raise RuntimeError(
                "服务账号模式需要 VERTEX_SA_JSON 指向密钥文件，"
                "或设置环境变量 GOOGLE_APPLICATION_CREDENTIALS"
            )
        kwargs: Dict[str, Any] = {"vertexai": True, "project": project, "location": location}
    else:
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY 为空：请在 %s\\.env 中填入 Key（AIza... 或 AQ... 开头）后重启服务"
                % os.path.dirname(os.path.abspath(__file__))
            )
        kwargs = {"vertexai": True, "api_key": key}
        if project and location:
            kwargs.update(project=project, location=location)

    # 出站代理：本机 Python 进程访问 Google 必须走代理（ZCode -> 本代理是 localhost，不受影响）
    proxy = (
        os.getenv("VERTEX_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or ""
    ).strip()
    if proxy:
        # 显式传给 httpx，避免依赖 trust_env 的解析细节
        kwargs["http_options"] = types.HttpOptions(
            client_args={"proxy": proxy, "trust_env": True},
            async_client_args={"proxy": proxy, "trust_env": True},
            timeout=int(os.getenv("VERTEX_TIMEOUT_MS", "600000")),
        )

    _client = genai.Client(**kwargs)
    _client_sig = sig
    log.info(
        "genai.Client 已初始化 (mode=%s, project=%s, proxy=%s)",
        mode,
        project or "express",
        proxy or "direct",
    )
    return _client


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # 忽略 stream_options 等多余字段

    model: Optional[str] = None
    messages: List[Dict[str, Any]] = []
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stop: Optional[Any] = None
    response_format: Optional[Dict[str, Any]] = None


# --------------------------------------------------------------------------
# OpenAI <-> Gemini 消息转换
# --------------------------------------------------------------------------
def _part_from_block(block: Dict[str, Any]) -> Optional[types.Part]:
    """OpenAI content 数组元素 -> google-genai Part"""
    btype = (block.get("type") or "").lower()

    if btype in ("text", "input_text"):
        text = block.get("text") or ""
        return types.Part(text=text) if text else None

    if btype == "image_url":
        url = (block.get("image_url") or {}).get("url") or ""
        if url.startswith("data:") and ";base64," in url:
            head, b64 = url.split(";base64,", 1)
            mime = head[5:] or "image/png"
            return types.Part(
                inline_data=types.Blob(mime_type=mime, data=base64.b64decode(b64))
            )
        if url:
            # 远程 URL：Vertex 需要先取字节，这里交给 SDK 的 URI 能力
            return types.Part(file_data=types.FileData(file_uri=url, mime_type="image/png"))
        return None

    return None


def _content_parts(content: Any) -> List[types.Part]:
    if content is None:
        return []
    if isinstance(content, str):
        return [types.Part(text=content)] if content else []
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                if block:
                    parts.append(types.Part(text=block))
            elif isinstance(block, dict):
                p = _part_from_block(block)
                if p is not None:
                    parts.append(p)
        return parts
    return [types.Part(text=str(content))]


def _as_text(content: Any) -> str:
    """把 tool 结果/多模态 content 压平成纯文本"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "\n".join(chunks)
    return str(content)


def build_contents(
    messages: List[Dict[str, Any]]
) -> Tuple[Optional[types.Content], List[types.Content]]:
    """返回 (system_instruction, contents)，含 tool_calls / tool 结果的双向映射"""
    system_chunks: List[str] = []
    contents: List[types.Content] = []
    call_id_to_name: Dict[str, str] = {}

    # 一轮工具调用可能包含多个并行调用（例如一次读 5 个文件）。
    # Gemini 要求这些结果【必须在同一个 turn 里，且数量与调用数一一对应】，
    # 拆成多个 user turn 会报 "number of function response parts is equal to ..."。
    call_order: List[Tuple[str, str]] = []   # 本轮调用的 (id, name)，保持模型发起顺序
    call_results: Dict[str, str] = {}
    open_turn = False

    def flush_tool_turn() -> None:
        nonlocal call_order, call_results, open_turn
        if not open_turn:
            return
        out_parts: List[types.Part] = []
        for cid, cname in call_order:
            text = call_results.pop(cid, None)
            if text is None:
                log.warning("工具 %s 缺少执行结果，已补占位以对齐调用数", cname)
                text = "(客户端未返回该工具的执行结果)"
            out_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=cname, response={"result": text}
                    )
                )
            )
        if out_parts:
            contents.append(types.Content(role="user", parts=out_parts))
        call_order, call_results, open_turn = [], {}, False

    for msg in messages or []:
        role = (msg.get("role") or "user").lower()
        parts = _content_parts(msg.get("content"))

        # 部分客户端（Claude 风格）把 system 塞进 content 数组
        if role in ("system", "developer"):
            system_chunks.extend(p.text for p in parts if getattr(p, "text", None))
            continue

        # 遇到非工具消息说明上一轮工具结果已经齐了，整体落盘
        if role != "tool":
            flush_tool_turn()

        # 工具执行结果：OpenAI 的 role=tool -> Gemini 的 functionResponse
        if role == "tool":
            tcid = str(msg.get("tool_call_id") or "")
            text = _as_text(msg.get("content"))
            if open_turn:
                if tcid in dict(call_order):
                    call_results[tcid] = text
                else:
                    log.warning("工具结果 tool_call_id=%r 不在本轮调用列表里，已丢弃", tcid)
            else:
                # 没有对应的调用轮（异常情况），单独成一轮
                name = call_id_to_name.get(tcid) or "tool_result"
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=name, response={"result": text}
                                )
                            )
                        ],
                    )
                )
            continue

        # 助手发起工具调用：OpenAI 的 tool_calls -> Gemini 的 functionCall
        tool_calls = msg.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            call_parts: List[types.Part] = list(parts)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                fname = fn.get("name")
                if not fname:
                    continue
                try:
                    fargs = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    fargs = {"_raw": fn.get("arguments")}
                if not isinstance(fargs, dict):
                    fargs = {"value": fargs}
                cached = _SIG_CACHE.get(str(tc.get("id") or ""))
                if cached is not None:
                    # 用当初下发的原始 part，把 thought_signature 一并还原
                    call_parts.append(cached)
                else:
                    call_parts.append(
                        types.Part(function_call=types.FunctionCall(name=fname, args=fargs))
                    )
                if tc.get("id"):
                    call_id_to_name[str(tc["id"])] = fname
            if call_parts:
                contents.append(types.Content(role="model", parts=call_parts))
                call_order = []
                for idx, tc in enumerate(tool_calls):
                    cname = (tc.get("function") or {}).get("name")
                    if not cname:
                        continue
                    cid = str(tc.get("id") or "") or ("_noid_%d" % idx)
                    call_order.append((cid, cname))
                call_results = {}
                open_turn = bool(call_order)
            continue

        grole = "model" if role == "assistant" else "user"
        if not parts:
            continue

        # Gemini 要求 user/model 交替：连续的相同角色合并。
        # 但含 functionResponse 的 turn 绝不能并入正文，否则 Vertex 会报
        # "Requests ending with a model turn are not supported"（实测 3.8-flash/4-argon 必现）
        last = contents[-1] if contents else None
        mergeable = (
            last is not None
            and last.role == grole
            and not any(getattr(p, "function_response", None) for p in (last.parts or []))
        )
        if mergeable:
            last.parts.extend(parts)  # type: ignore[union-attr]
        else:
            contents.append(types.Content(role=grole, parts=parts))

    flush_tool_turn()

    if contents and contents[0].role != "user":
        contents.insert(0, types.Content(role="user", parts=[types.Part(text="Continue.")]))

    system = None
    system_text = "\n\n".join(c for c in system_chunks if c.strip())
    if system_text:
        system = types.Content(parts=[types.Part(text=system_text)])
    return system, contents


# 这些 JSON Schema 关键字 Gemini 不接受，直接剔掉，否则会 400
_SCHEMA_DROP = {
    "$schema",
    "$id",
    "$comment",
    "additionalProperties",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "strict",
    "definitions",
    "$defs",
}


def _clean_schema(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _SCHEMA_DROP:
                continue
            out[k] = _clean_schema(v)
        return out
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


_tools_dumped = False


def convert_tools(body: Dict[str, Any]) -> Tuple[Optional[List[types.Tool]], Any]:
    """OpenAI tools / tool_choice -> (Gemini Tool 列表, ToolConfig)"""
    global _tools_dumped
    raw_tools = body.get("tools") or []
    decls: List[types.FunctionDeclaration] = []
    for t in raw_tools:
        if not isinstance(t, dict) or t.get("type") not in (None, "function"):
            continue
        fn = t.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        decls.append(
            types.FunctionDeclaration(
                name=name,
                description=fn.get("description") or "",
                parameters_json_schema=_clean_schema(params),
            )
        )

    if not decls:
        return None, None

    # 第一次拿到工具清单时打印一次，便于排查"模型选错工具"
    if not _tools_dumped:
        _tools_dumped = True
        log.info("工具清单(%d): %s", len(decls), [d.name for d in decls])

    mode, allowed = "AUTO", None
    choice = body.get("tool_choice")
    if choice == "none":
        mode = "NONE"
    elif choice == "required":
        mode = "ANY"
    elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
        mode = "ANY"
        allowed = [choice["function"]["name"]]

    tool_config = types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode=mode, allowed_function_names=allowed
        )
    )
    return [types.Tool(function_declarations=decls)], tool_config


def extract_tool_calls(resp: Any) -> List[Dict[str, Any]]:
    """从 Gemini 响应里抽出 functionCall，转成 OpenAI tool_calls 结构"""
    calls: List[Dict[str, Any]] = []
    try:
        parts = resp.candidates[0].content.parts or []
    except Exception:
        return calls

    for part in parts:
        fc = getattr(part, "function_call", None)
        if not fc or not getattr(fc, "name", None):
            continue
        args = getattr(fc, "args", None)
        call_id = "call_" + uuid.uuid4().hex[:24]
        _remember_part(call_id, part)  # 记下带 thought_signature 的原始 part
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": fc.name,
                    "arguments": json.dumps(args if args is not None else {}, ensure_ascii=False),
                },
            }
        )
    return calls


def normalize_model(name: Optional[str]) -> str:
    name = (name or DEFAULT_MODEL).strip()
    for prefix in ("google/", "models/", "vertex_ai/", "vertex/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    name = name or DEFAULT_MODEL
    return MODEL_ALIASES.get(name, name)


def _finish_reason(resp: Any) -> str:
    try:
        fr = resp.candidates[0].finish_reason
    except Exception:
        return "stop"
    fr_name = getattr(fr, "name", str(fr))
    return "length" if "MAX_TOKENS" in fr_name else "stop"


def _usage(resp: Any) -> Dict[str, int]:
    um = getattr(resp, "usage_metadata", None)
    p = int(getattr(um, "prompt_token_count", 0) or 0)
    c = int(getattr(um, "candidates_token_count", 0) or 0)
    t = int(getattr(um, "total_token_count", 0) or 0) or (p + c)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": t}


# --------------------------------------------------------------------------
# 服务端内置工具桥接
# Gemini 不允许 google_search 与 functionDeclarations 混用（实测 400），
# 而 ZCode 把 Google:web_search / Google:python_interpreter 当普通函数发过来，
# 前端又没有实现 -> 只能由本代理代执行，再把结果回灌给模型。
# --------------------------------------------------------------------------
_SERVER_TOOL_ALIASES: Dict[str, set] = {
    "search": {
        "google:web_search",
        "google_web_search",
        "web_search",
        "google_search",
        "googlesearch",
        "search_web",
    },
    "code": {
        "google:python_interpreter",
        "google_python_interpreter",
        "python_interpreter",
        "code_execution",
        "google:code_execution",
    },
}


def server_tool_kind(name: str) -> Optional[str]:
    low = (name or "").strip().lower()
    if low in _SERVER_TOOL_ALIASES["search"]:
        return "search"
    # 代码执行默认不代执行：Gemini 的 code_execution 跑在沙箱里，
    # 能算出结果但不会把文件真正写到本机，代执行会让模型误以为文件已生成。
    if low in _SERVER_TOOL_ALIASES["code"] and _flag("BRIDGE_CODE_EXEC", False):
        return "code"
    return None


def _first_arg_text(args: Any) -> str:
    if isinstance(args, dict):
        for key in ("query", "q", "search_query", "url", "prompt", "code", "input"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in args.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(args, ensure_ascii=False)
    if isinstance(args, str):
        return args
    return json.dumps(args, ensure_ascii=False)


def run_server_tool(client: Any, model: str, kind: str, args: Any) -> str:
    """在网关内部代执行服务端工具（搜索 / 代码执行）。"""
    need = _first_arg_text(args)
    if kind == "search":
        tools = [types.Tool(google_search=types.GoogleSearch())]
        prompt = (
            "请联网检索以下需求，用简洁中文给出事实性结果。"
            "只给结论与数据，不要寒暄，不要描述你的检索过程：\n" + need
        )
        # 搜索固定用快模型：实测 gemini-4-argon 带 grounding 要 49~170 秒，会把客户端卡死
        use_model = (os.getenv("BRIDGE_SEARCH_MODEL") or "gemini-2.5-flash").strip() or model
    else:
        tools = [types.Tool(code_execution=types.ToolCodeExecution())]
        prompt = "请用 Python 代码执行完成以下需求，并给出执行结果，不要只贴代码：\n" + need
        use_model = model

    resp = _generate(
        client,
        use_model,
        prompt,
        types.GenerateContentConfig(tools=tools, max_output_tokens=4096),
    )
    text = (resp.text or "").strip()
    log.info("服务端工具命中 %s（模型 %s）", kind, use_model)
    return text or "(工具没有返回内容)"


# --------------------------------------------------------------------------
# 上游重试：429/503/500 自动退避重试，避免一次限流就打挂整个 Agent 回合
# --------------------------------------------------------------------------
def _retry_cfg() -> Tuple[int, float, float]:
    def _num(key: str, default: str) -> float:
        try:
            return float(os.getenv(key, default) or default)
        except ValueError:
            return float(default)

    return (
        max(1, int(_num("RETRY_MAX_ATTEMPTS", "3"))),
        _num("RETRY_BASE_DELAY", "8"),
        _num("RETRY_MAX_DELAY", "60"),
    )


def _retryable(exc: BaseException) -> bool:
    s = str(exc)

    return any(
        k in s
        for k in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL")
    )


def _backoff(attempt: int, base: float, cap: float) -> float:
    delay = min(cap, base * (2 ** attempt))
    return delay + random.uniform(0, 1)


def _generate(client: Any, model: str, contents: Any, config: Any) -> Any:
    """带退避重试的非流式调用。"""
    attempts, base, cap = _retry_cfg()
    for i in range(attempts):
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=config
            )
        except Exception as exc:
            if not _retryable(exc) or i >= attempts - 1:
                raise
            wait = _backoff(i, base, cap)
            log.warning("上游限流/故障，%.0f 秒后重试（%d/%d）: %s",
                        wait, i + 1, attempts, str(exc)[:80])
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _open_stream(client: Any, model: str, contents: Any, config: Any) -> Tuple[Any, Any]:
    """建立上游流并预取首帧：限流在这里重试，硬错误在响应头之前抛出。"""
    attempts, base, cap = _retry_cfg()
    for i in range(attempts):
        try:
            iterator = client.models.generate_content_stream(
                model=model, contents=contents, config=config
            )
            first = next(iterator, None)  # 触发首次请求，错误在这里暴露
            return iterator, first
        except Exception as exc:
            if not _retryable(exc) or i >= attempts - 1:
                raise
            wait = _backoff(i, base, cap)
            log.warning("上游限流/故障，%.0f 秒后重试（%d/%d）: %s",
                        wait, i + 1, attempts, str(exc)[:80])
            time.sleep(wait)
    raise RuntimeError("unreachable")


def generate_with_bridge(
    client: Any,
    model: str,
    contents: List[types.Content],
    config: Optional[types.GenerateContentConfig],
    enabled: bool,
    max_rounds: int = 3,
) -> Any:
    """带服务端工具自动代执行的生成；遇到前端工具则原样交回客户端。"""
    work = list(contents)
    resp: Any = None

    for round_no in range(max_rounds + 1):
        resp = _generate(client, model, work, config)
        if not enabled:
            return resp

        calls = extract_tool_calls(resp)
        if not calls:
            return resp

        bridge = [c for c in calls if server_tool_kind(c["function"]["name"])]
        if not bridge:
            return resp
        if len(bridge) != len(calls):
            log.warning("本轮混有服务端工具与前端工具调用，交回客户端处理")
            return resp
        if round_no >= max_rounds:
            log.warning("服务端工具桥接已达上限 %d 轮，直接返回", max_rounds)
            return resp

        try:
            model_parts = resp.candidates[0].content.parts
        except Exception:
            return resp

        work.append(types.Content(role="model", parts=model_parts))
        resp_parts: List[types.Part] = []
        for c in calls:
            fn = c["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            kind = server_tool_kind(fn["name"]) or "search"
            text = run_server_tool(client, model, kind, args)
            log.info("服务端代执行 %s -> %d 字符", fn["name"], len(text))
            resp_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=fn["name"], response={"result": text}
                    )
                )
            )
        work.append(types.Content(role="user", parts=resp_parts))

    return resp


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _make_sanitizer() -> Sanitizer:
    """按 .env 开关构造本次请求专用的脱敏器（映射逐请求隔离）。"""
    if not _flag("SANITIZE_ENABLED", True):
        return Sanitizer(secrets=False, pii=False, network=False, restore=False)
    return Sanitizer(
        secrets=_flag("SANITIZE_SECRETS", True),
        pii=_flag("SANITIZE_PII", True),
        network=_flag("SANITIZE_NETWORK", False),
        restore=_flag("SANITIZE_RESTORE", True),
    )


def _err(message: str, status: int = 500, etype: str = "proxy_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": etype, "code": status}},
    )


def _upstream_msg(exc: Exception, model: str) -> str:
    """把 SDK 异常翻译成对客户端有用的提示。"""
    text = "%s: %s" % (type(exc).__name__, exc)
    s = str(exc)
    if "NOT_FOUND" in s or "was not found" in s:
        text += (
            " | 提示：project/location 下找不到模型 %s，当前环境可用模型为 %s"
            % (model, ", ".join(MODELS))
        )
    elif "UNAUTHENTICATED" in s or "API key not valid" in s or " 401 " in s:
        text += " | 提示：GEMINI_API_KEY 无效或不是 Vertex 可用的 Key"
    elif "PERMISSION_DENIED" in s or " 403 " in s:
        text += " | 提示：Key 无权访问该模型，或该 API 未在 GCP 项目启用"
    elif "thought_signature" in s:
        text += (
            " | 提示：该模型要求回传工具调用时带 thought_signature，"
            "而代理重启后缓存已丢（会话里的旧 tool_call 找不回签名）。"
            "请新建一个对话继续；或用 gemini-2.5 系列（不强制签名）"
        )
    elif "429" in s or "RESOURCE_EXHAUSTED" in s:
        text += (
            " | 提示：上游配额暂时用尽。搜索类请求会额外消耗 2 倍调用；"
            "稍等 30~60 秒重试，或改用 gemini-2.5-flash，或设 SERVER_TOOL_BRIDGE=0 关掉代执行"
        )
    return text


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "vertex-openai-proxy",
        "base_url": "http://%s:%d/v1" % (HOST, PORT),
        "key_loaded": bool((os.getenv("GEMINI_API_KEY") or "").strip()),
        "models": MODELS,
    }


@app.get("/v1/models")
def list_models() -> Dict[str, Any]:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "google"}
            for m in MODELS
        ],
    }


@app.get("/v1/models/{model_id}")
def get_model(model_id: str) -> Dict[str, Any]:
    return {"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "google"}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    body = req.model_dump()
    model = normalize_model(body.get("model"))
    stream = bool(body.get("stream"))

    # 出站脱敏：在转换成 Gemini 结构之前，先把密钥/口令/隐私换成占位符
    san = _make_sanitizer()
    san.scrub_messages(body.get("messages") or [])
    if san.total:
        log.info("脱敏 %d 处 %s", san.total, san.counts)
    if _flag("SANITIZE_DEBUG") and san.total:
        log.info("出站内容: %s", json.dumps(body.get("messages"), ensure_ascii=False)[:1000])

    system, contents = build_contents(body.get("messages") or [])

    if not contents:
        return _err("messages 不能为空", 400, "invalid_request_error")

    gemini_tools, tool_config = convert_tools(body)
    if body.get("tools") and not gemini_tools:
        log.warning("客户端传了 tools，但没有可转换的 function 声明（已忽略）")

    cfg: Dict[str, Any] = {}
    if gemini_tools:
        cfg["tools"] = gemini_tools
        if tool_config is not None:
            cfg["tool_config"] = tool_config
    if system is not None:
        cfg["system_instruction"] = system
    if body.get("temperature") is not None:
        cfg["temperature"] = float(body["temperature"])
    if body.get("top_p") is not None:
        cfg["top_p"] = float(body["top_p"])
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    if max_tokens:
        cfg["max_output_tokens"] = int(max_tokens)
    stop = body.get("stop")
    if isinstance(stop, str):
        cfg["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        cfg["stop_sequences"] = [str(s) for s in stop]
    if (body.get("response_format") or {}).get("type") == "json_object":
        cfg["response_mime_type"] = "application/json"

    # 思考预算：2.5-pro 默认一直思考（首字延迟高），可用 .env 的 THINKING_BUDGET 压低
    budget = (os.getenv("THINKING_BUDGET") or "").strip()
    if budget:
        try:
            cfg["thinking_config"] = types.ThinkingConfig(
                thinking_budget=int(budget),
                include_thoughts=_flag("INCLUDE_THOUGHTS", False),
            )
        except ValueError:
            log.warning("THINKING_BUDGET=%s 不是整数，已忽略", budget)

    config = types.GenerateContentConfig(**cfg) if cfg else None
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    bridge_enabled = _flag("SERVER_TOOL_BRIDGE", True)
    try:
        bridge_rounds = int(os.getenv("BRIDGE_MAX_ROUNDS", "3") or 3)
    except ValueError:
        bridge_rounds = 3

    try:
        client = get_client()
    except Exception as exc:
        return _err(str(exc), 500, "config_error")

    # ---------------- 非流式 ----------------
    if not stream:
        try:
            resp = generate_with_bridge(
                client, model, contents, config, bridge_enabled, bridge_rounds
            )
            text = san.restore(resp.text or "") or ""
            tool_calls = extract_tool_calls(resp)
            for tc in tool_calls:
                tc["function"]["arguments"] = san.restore(tc["function"]["arguments"])

            message: Dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                message["tool_calls"] = tool_calls
                message["content"] = text or None

            payload = {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if tool_calls else _finish_reason(resp),
                    }
                ],
                "usage": _usage(resp),
            }
            log.info(
                "POST stream=False model=%s chars=%d tool_calls=%d tools_in=%d names=%s",
                model,
                len(text),
                len(tool_calls),
                len(body.get("tools") or []),
                [c["function"]["name"] for c in tool_calls],
            )
            return JSONResponse(payload)
        except Exception as exc:
            log.exception("非流式调用失败")
            return _err(_upstream_msg(exc, model), 502, "upstream_error")

    # ---------------- 流式 SSE ----------------
    def sse(obj: Any) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> Dict[str, Any]:
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    # 先建立上游流（含限流重试）：这样 429/400 能在 SSE 开始前以正常 HTTP 错误返回，
    # 客户端才能显示具体原因，而不是笼统的 reason=unknown
    try:
        stream_iter, stream_first = await run_in_threadpool(
            _open_stream, client, model, contents, config
        )
    except Exception as exc:
        log.exception("流式建流失败")
        return _err(_upstream_msg(exc, model), 502, "upstream_error")

    def event_stream():
        last_usage: Dict[str, int] = {}
        restorer = StreamRestorer(san)
        pending_calls: List[Dict[str, Any]] = []
        native_parts: List[types.Part] = []
        try:
            # 首帧先给 role，兼容严格模式客户端
            yield sse(chunk({"role": "assistant", "content": ""}))
            pieces = (
                itertools.chain([stream_first], stream_iter)
                if stream_first is not None
                else stream_iter
            )
            for piece in pieces:
                last_usage = _usage(piece) or last_usage

                # 先把本帧里的 functionCall 收集起来（Gemini 一般整块下发）
                try:
                    parts = piece.candidates[0].content.parts or []
                except Exception:
                    parts = []
                # 原样保留 part（含 thought_signature），回灌时缺了它 4-argon 会 400
                native_parts.extend(parts)
                for part in parts:
                    fc = getattr(part, "function_call", None)
                    if not fc or not getattr(fc, "name", None):
                        continue
                    args = getattr(fc, "args", None) or {}
                    if (
                        pending_calls
                        and pending_calls[-1]["function"]["name"] == fc.name
                        and getattr(fc, "will_continue", False)
                    ):
                        pending_calls[-1]["_args"].update(args)
                    else:
                        pending_calls.append(
                            {
                                "id": "call_" + uuid.uuid4().hex[:24],
                                "type": "function",
                                "function": {"name": fc.name, "arguments": "{}"},
                                "_args": dict(args),
                                "_part": part,
                            }
                        )

                try:
                    text = piece.text or ""
                except Exception:
                    text = ""
                text = restorer.feed(text) if text else ""
                if text:
                    yield sse(chunk({"content": text}))

            tail = restorer.flush()
            if tail:
                yield sse(chunk({"content": tail}))

            # 模型调用了服务端内置工具：本机代执行后继续，不把调用交给前端
            if (
                pending_calls
                and bridge_enabled
                and all(server_tool_kind(c["function"]["name"]) for c in pending_calls)
            ):
                work = list(contents)
                call_parts: List[types.Part] = []
                fn_parts: List[types.Part] = []
                for c in pending_calls:
                    fn = c["function"]
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    call_parts.append(
                        types.Part(function_call=types.FunctionCall(name=fn["name"], args=args))
                    )
                    kind = server_tool_kind(fn["name"]) or "search"
                    result = run_server_tool(client, model, kind, args)
                    log.info("服务端代执行(流式) %s -> %d 字符", fn["name"], len(result))
                    fn_parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=fn["name"], response={"result": result}
                            )
                        )
                    )
                # 优先用原始 part（带 thought_signature），拿不到才退回重建
                work.append(
                    types.Content(role="model", parts=native_parts or call_parts)
                )
                work.append(types.Content(role="user", parts=fn_parts))

                resp2 = generate_with_bridge(
                    client, model, work, config, bridge_enabled, bridge_rounds
                )
                text2 = san.restore(resp2.text or "") or ""
                if text2:
                    yield sse(chunk({"content": text2}))
                pending_calls = extract_tool_calls(resp2)

            if pending_calls:
                deltas = []
                for i, tc in enumerate(pending_calls):
                    _remember_part(tc["id"], tc.pop("_part", None))
                    raw_args = tc.pop("_args", None)
                    if raw_args is not None:
                        tc["function"]["arguments"] = san.restore(
                            json.dumps(raw_args, ensure_ascii=False)
                        )
                    else:
                        tc["function"]["arguments"] = san.restore(
                            tc["function"]["arguments"]
                        )
                    tc["index"] = i
                    deltas.append(tc)
                yield sse(chunk({"tool_calls": deltas}))
                finish = "tool_calls"
            else:
                finish = "stop"

            final = chunk({}, finish)
            if (body.get("stream_options") or {}).get("include_usage") and last_usage:
                final["usage"] = last_usage
            yield sse(final)
            yield "data: [DONE]\n\n"
            log.info(
                "POST stream=True model=%s tool_calls=%d tools_in=%d names=%s",
                model,
                len(pending_calls),
                len(body.get("tools") or []),
                [c["function"]["name"] for c in pending_calls],
            )
        except Exception as exc:
            log.exception("流式调用失败")
            yield sse({"error": {"message": _upstream_msg(exc, model),
                                 "type": "upstream_error"}})
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


if __name__ == "__main__":
    import uvicorn

    log.info("Base URL: http://%s:%d/v1", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
