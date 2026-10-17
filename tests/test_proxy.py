# -*- coding: utf-8 -*-
"""
vertex_openai_proxy 回归测试
============================
全部使用假的上游客户端（FakeModels），不打真实 Vertex API，不需要 Key。

覆盖的都是实际踩过的坑：
1. 脱敏可逆 + 流式占位符拆包
2. 并行工具调用必须合并在同一个 turn（否则 400 number of function response parts...）
3. function_response 不能与正文合并（否则 400 ending with a model turn）
4. thought_signature 回传（3.8-flash / 4-argon 强制）
5. 服务端内置工具代执行（Google:web_search）
6. 429 自动重试
7. 硬错误以 HTTP 错误返回，而不是流里塞一个看不见的 error
8. 正常 SSE 流
9. 模型名归一化与别名
10. 脱敏后的内容真的没发出去
"""

from __future__ import annotations

import json
import types as pytypes

import pytest
from fastapi.testclient import TestClient

import vertex_openai_proxy as vp
from sanitizer import Sanitizer, StreamRestorer


# --------------------------------------------------------------------------
# 假上游
# --------------------------------------------------------------------------
def _ns(**kw):
    return pytypes.SimpleNamespace(**kw)


USAGE = _ns(prompt_token_count=10, candidates_token_count=5, total_token_count=15)


def _part(text=None, fc=None, signature=None):
    p = _ns(text=text, function_call=fc, function_response=None)
    if signature is not None:
        p.thought_signature = signature
    return p


def resp_text(text, finish="STOP"):
    return _ns(
        text=text,
        candidates=[
            _ns(content=_ns(parts=[_part(text=text)]), finish_reason=_ns(name=finish))
        ],
        usage_metadata=USAGE,
    )


def resp_calls(calls):
    """calls: [(name, args_dict), ...]"""
    parts = [
        _part(fc=_ns(name=name, args=args), signature=b"sig-%d" % i)
        for i, (name, args) in enumerate(calls)
    ]
    return _ns(
        text="",
        candidates=[_ns(content=_ns(parts=parts), finish_reason=_ns(name="STOP"))],
        usage_metadata=USAGE,
    )


def chunk(text):
    return _ns(
        text=text,
        candidates=[_ns(content=_ns(parts=[_part(text=text)]))],
        usage_metadata=None,
    )


class FakeModels:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []  # [(model, contents, config)]
        self.n = 0

    def _take(self):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def generate_content(self, model=None, contents=None, config=None):
        self.calls.append((model, contents, config))
        self.n += 1
        return self._take()

    def generate_content_stream(self, model=None, contents=None, config=None):
        self.calls.append((model, contents, config))
        self.n += 1
        item = self._take()
        if isinstance(item, BaseException):
            raise item

        def gen():
            for c in item:
                yield c

        return gen()


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


@pytest.fixture
def client():
    vp._SIG_CACHE.clear()
    return TestClient(vp.app)


@pytest.fixture
def fake(monkeypatch):
    """用法: fake([...]) 返回 FakeClient，并把它装到 get_client 上。"""

    def _install(script):
        fc = FakeClient(script)

        def _get():
            return fc

        monkeypatch.setattr(vp, "get_client", _get)
        return fc

    return _install


def _payload(**kw):
    body = {
        "model": "gemini-3.8-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    body.update(kw)
    return body


# --------------------------------------------------------------------------
# 1. 脱敏：可逆 + 流式拆包
# --------------------------------------------------------------------------
def test_sanitize_roundtrip():
    s = Sanitizer()
    raw = (
        "GEMINI_API_KEY=AQ.Ab8RN6JkLmNoPqRsTuVwXyZ0123456789abcd\n"
        "DB_PASSWORD=Passw0rd!2026\n"
        "mail alice@example.com tel 13800138000\n"
        "postgres://admin:hunter2@10.0.0.5:5432/prod\n"
    )
    masked = s.scrub(raw)
    assert "Passw0rd" not in masked
    assert "alice@example.com" not in masked
    assert "hunter2" not in masked
    assert "[[SECRET_1]]" in masked
    assert s.restore(masked) == raw


def test_stream_restorer_handles_split_placeholder():
    s = Sanitizer()
    s.scrub("token=ABCDEFGH1234567890abcd")
    r = StreamRestorer(s)
    out = "".join(r.feed(p) for p in ["token=", "[[SEC", "RET_", "1]]", " 结束"])
    out += r.flush()
    assert out == "token=ABCDEFGH1234567890abcd 结束"
    assert "[[" not in out


# --------------------------------------------------------------------------
# 2/3. 历史拼接结构
# --------------------------------------------------------------------------
def _call(i, name="Read"):
    return {
        "id": "call_%d" % i,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps({"path": "f%d.py" % i})},
    }


def test_parallel_calls_merged_into_one_turn():
    msgs = [{"role": "user", "content": "读 5 个文件"}]
    calls = [_call(i) for i in range(5)]
    msgs.append({"role": "assistant", "content": None, "tool_calls": calls})
    msgs += [
        {"role": "tool", "tool_call_id": "call_%d" % i, "content": "x%d" % i}
        for i in range(5)
    ]
    msgs.append({"role": "user", "content": "总结"})

    _system, contents = vp.build_contents(msgs)
    assert [c.role for c in contents] == ["user", "model", "user", "user"]
    assert len(contents[1].parts) == 5  # 5 个 function_call
    assert len(contents[2].parts) == 5  # 5 个 function_response 在同一个 turn
    assert all(p.function_response for p in contents[2].parts)


def test_missing_tool_result_is_padded():
    msgs = [{"role": "user", "content": "读 5 个文件"}]
    calls = [_call(i) for i in range(5)]
    msgs.append({"role": "assistant", "content": None, "tool_calls": calls})
    # 只回 4 个结果
    msgs += [
        {"role": "tool", "tool_call_id": "call_%d" % i, "content": "x"}
        for i in range(4)
    ]

    _system, contents = vp.build_contents(msgs)
    assert len(contents[2].parts) == 5, "缺结果必须补占位，否则上游 400"


def test_function_response_not_merged_with_text():
    msgs = [
        {"role": "user", "content": "读一个文件"},
        {"role": "assistant", "content": None, "tool_calls": [_call(0)]},
        {"role": "tool", "tool_call_id": "call_0", "content": "x"},
        {"role": "user", "content": "然后呢"},
    ]
    _system, contents = vp.build_contents(msgs)
    assert len(contents) == 4, "工具结果不能与正文合并进同一个 turn"
    assert len(contents[2].parts) == 1 and contents[2].parts[0].function_response


# --------------------------------------------------------------------------
# 4. thought_signature 回传
# --------------------------------------------------------------------------
def test_thought_signature_reused_when_client_replays_history(client, fake):
    fc = fake([resp_calls([("Bash", {"command": "ls"})]), resp_text("ok")])

    r1 = client.post(
        "/v1/chat/completions",
        json=_payload(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "description": "run",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                        },
                    },
                }
            ],
        ),
    )
    assert r1.status_code == 200
    call = r1.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "Bash"

    # 把模型这轮调用 + 结果回传，模拟客户端第二步
    r2 = client.post(
        "/v1/chat/completions",
        json=_payload(
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": "a.py"},
                {"role": "user", "content": "列出文件名"},
            ],
        ),
    )
    assert r2.status_code == 200

    contents = fc.models.calls[-1][1]
    model_turn = [c for c in contents if c.role == "model"][0]
    assert model_turn.parts[0].thought_signature == b"sig-0", "回传时必须带上原始签名"


# --------------------------------------------------------------------------
# 5. 服务端工具代执行
# --------------------------------------------------------------------------
def test_server_tool_bridge_executes_search(client, fake, monkeypatch):
    monkeypatch.setenv("BRIDGE_SEARCH_MODEL", "gemini-2.5-flash")
    fc = fake(
        [
            resp_calls([("Google:web_search", {"query": "沈阳天气"})]),
            resp_text("沈阳 25 度，多云"),
            resp_text("沈阳今天 25 度，多云。"),
        ]
    )

    r = client.post(
        "/v1/chat/completions",
        json=_payload(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "Google:web_search",
                        "description": "搜索",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "description": "run",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                        },
                    },
                },
            ],
        ),
    )
    assert r.status_code == 200
    body = r.json()
    msg = body["choices"][0]["message"]
    assert msg.get("tool_calls") in (None, []), "服务端工具不应交给客户端执行"
    assert "25" in (msg.get("content") or "")
    assert fc.models.n == 3, "应为：决策 -> 代执行搜索 -> 最终作答"
    assert fc.models.calls[1][0] == "gemini-2.5-flash", "搜索应走快模型"


# --------------------------------------------------------------------------
# 6. 429 重试
# --------------------------------------------------------------------------
def test_retry_on_429(client, fake, monkeypatch):
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    monkeypatch.setenv("RETRY_MAX_DELAY", "0")
    fc = fake(
        [Exception("429 RESOURCE_EXHAUSTED. rate limited"), resp_text("retried ok")]
    )

    r = client.post("/v1/chat/completions", json=_payload())
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "retried ok"
    assert fc.models.n == 2


# --------------------------------------------------------------------------
# 7. 硬错误 -> HTTP 错误（不发 SSE）
# --------------------------------------------------------------------------
def test_hard_error_returns_http_error_not_sse(client, fake, monkeypatch):
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "1")
    fake([Exception("400 INVALID_ARGUMENT. boom")])

    r = client.post("/v1/chat/completions", json=_payload(stream=True))
    assert r.status_code == 502
    assert "application/json" in r.headers["content-type"]
    assert "400" in r.json()["error"]["message"]


# --------------------------------------------------------------------------
# 8. 正常流式
# --------------------------------------------------------------------------
def test_stream_sse(client, fake):
    fake([[chunk("1\n"), chunk("2\n"), chunk("3")]])
    r = client.post("/v1/chat/completions", json=_payload(stream=True))
    assert r.status_code == 200
    text = r.text
    assert '"content": "1\\n"' in text
    assert '"finish_reason": "stop"' in text
    assert text.strip().endswith("[DONE]")


# --------------------------------------------------------------------------
# 9. 模型名归一化
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "given,expected",
    [
        ("google/gemini-4-argon", "gemini-4-argon"),
        ("models/gemini-3.8-flash", "gemini-3.8-flash"),
        ("google/gemini-2.0-flash", "gemini-2.5-flash"),  # 别名改写
        ("gemini-1.5-pro", "gemini-2.5-pro"),
    ],
)
def test_model_normalization(given, expected):
    assert vp.normalize_model(given) == expected


# --------------------------------------------------------------------------
# 10. 脱敏后的内容真的没发出去
# --------------------------------------------------------------------------
def test_secret_masked_before_upstream_and_restored(client, fake):
    fc = fake([resp_text("DB_PASSWORD=[[SECRET_1]]")])
    r = client.post(
        "/v1/chat/completions",
        json=_payload(
            messages=[{"role": "user", "content": "DB_PASSWORD=Passw0rd!2026"}],
        ),
    )
    assert r.status_code == 200

    sent = fc.models.calls[0][1][0].parts[0].text
    assert "Passw0rd" not in sent, "明文密码不能出现在出站内容里"
    assert "[[SECRET_1]]" in sent
    # 回包再把占位符换回原文
    assert r.json()["choices"][0]["message"]["content"] == "DB_PASSWORD=Passw0rd!2026"
