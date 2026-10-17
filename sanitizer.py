# -*- coding: utf-8 -*-
"""
sanitizer.py
============
轻量脱敏层：出站前把「密钥 / 口令 / 隐私」替换成可还原的占位符，响应回来再换回原文。

设计取舍
--------
* 只处理**高置信度**的模式，避免把代码里的变量名、路径、URL 搞得面目全非。
* 占位符形如 ``[[SECRET_1]]``，模型基本会原样回显，因此响应里可以精确还原。
* 每轮请求单独一份映射，不跨请求复用（避免把上一轮的原文带进这一轮）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# 占位符（容忍模型输出时插入的空格）
_PH_BODY = r"\[\[\s*([A-Za-z]+)\s*_\s*(\d+)\s*\]\]"
PLACEHOLDER_RE = re.compile(_PH_BODY, re.I)
# 判断流式尾部是否还有半个占位符
OPEN_RE = re.compile(r"\[\[[A-Z_0-9]*$")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        if not ch.isdigit():
            return False
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------- 规则表
SECRET_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    # PEM 私钥整块
    (
        "PRIVATEKEY",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.S,
        ),
    ),
    # 常见云厂商 / 平台令牌
    (
        "SECRET",
        re.compile(
            r"\b(?:AIza[0-9A-Za-z_\-]{30,}"
            r"|AQ\.[A-Za-z0-9_\-.]{20,}"
            r"|sk-[A-Za-z0-9_\-]{20,}"
            r"|sk-ant-[A-Za-z0-9_\-]{20,}"
            r"|gh[pousr]_[A-Za-z0-9]{20,}"
            r"|github_pat_[A-Za-z0-9_]{20,}"
            r"|glpat-[A-Za-z0-9_\-]{16,}"
            r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
            r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
            r"|ya29\.[A-Za-z0-9_\-.]{20,}"
            r"|hf_[A-Za-z0-9]{20,}"
            r"|npm_[A-Za-z0-9]{20,})\b"
        ),
    ),
    # JWT
    (
        "TOKEN",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    ),
    # Authorization 头里的凭据
    (
        "TOKEN",
        re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9\-._~+/]{12,}=*", re.I),
    ),
]

# KEY=value / password: xxx 形式（保留变量名，只遮值）
KV_RE = re.compile(
    r"([\w.\-]{0,40}(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|credential)[\w.\-]{0,20})"
    r"(\s*[=:]\s*)([\"']?)((?!\[\[)[^\s\"',;]{4,})\3",
    re.I,
)

# user:password@host 连接串
DSN_RE = re.compile(r"([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]{3,})(@)", re.I)

PII_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    (
        "IDCARD",
        re.compile(
            r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
            r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])"
        ),
    ),
    ("CARD", re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)")),
]

NETWORK_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    (
        "IP",
        re.compile(
            r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])"
        ),
    ),
]


class Sanitizer:
    """一次请求一份实例，天然做到映射隔离。"""

    def __init__(
        self,
        secrets: bool = True,
        pii: bool = True,
        network: bool = False,
        restore: bool = True,
    ) -> None:
        self.use_secrets = secrets
        self.use_pii = pii
        self.use_network = network
        self.restore_enabled = restore
        self.map: Dict[str, str] = {}          # "SECRET_1" -> 原文
        self.counts: Dict[str, int] = {}       # 标签 -> 次数

    # ---------------------------------------------------------- 内部
    def _ph(self, label: str, value: str) -> str:
        # 占位符本身绝不二次脱敏，否则会套娃、还原不回去
        if PLACEHOLDER_RE.fullmatch(value):
            return value
        for key, original in self.map.items():
            if original == value:
                return "[[%s]]" % key
        self.counts[label] = self.counts.get(label, 0) + 1
        key = "%s_%d" % (label, self.counts[label])
        self.map[key] = value
        return "[[%s]]" % key

    def _kv(self, m: "re.Match[str]") -> str:
        quote = m.group(3)
        return m.group(1) + m.group(2) + quote + self._ph("SECRET", m.group(4)) + quote

    def _dsn(self, m: "re.Match[str]") -> str:
        return m.group(1) + self._ph("PASSWORD", m.group(2)) + m.group(3)

    def _card(self, m: "re.Match[str]") -> str:
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        # 只有通过 Luhn 校验才当银行卡，避免误伤长数字
        return self._ph("CARD", raw) if _luhn_ok(digits) else raw

    # ---------------------------------------------------------- 对外
    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def scrub(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return text

        if self.use_secrets:
            # 先处理带变量名的赋值和连接串，再扫孤立令牌，
            # 这样 KEY=xxx 只会被替换一次（避免占位符被再次包装）
            text = DSN_RE.sub(self._dsn, text)
            text = KV_RE.sub(self._kv, text)
            for label, rx in SECRET_RULES:
                text = rx.sub(lambda m, l=label: self._ph(l, m.group(0)), text)

        if self.use_pii:
            for label, rx in PII_RULES:
                handler = (
                    self._card
                    if label == "CARD"
                    else (lambda m, l=label: self._ph(l, m.group(0)))
                )
                text = rx.sub(handler, text)

        if self.use_network:
            for label, rx in NETWORK_RULES:
                text = rx.sub(lambda m, l=label: self._ph(l, m.group(0)), text)

        return text

    def scrub_messages(self, messages: List[dict]) -> List[dict]:
        """就地脱敏 OpenAI messages 里的所有文本块（含 tool_calls 参数）。"""
        for msg in messages or []:
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = self.scrub(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        block["text"] = self.scrub(block["text"])

            # 助手发起的工具调用参数里可能带密钥（例如写配置文件的参数）
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if isinstance(fn.get("arguments"), str):
                    fn["arguments"] = self.scrub(fn["arguments"])
        return messages

    def restore(self, text: Optional[str]) -> Optional[str]:
        if not text or not self.restore_enabled or not self.map:
            return text

        def _r(m: "re.Match[str]") -> str:
            # 模型可能把占位符改成小写，这里统一按大写查表
            return self.map.get("%s_%s" % (m.group(1).upper(), m.group(2)), m.group(0))

        return PLACEHOLDER_RE.sub(_r, text)


class StreamRestorer:
    """流式场景：占位符可能被拆到两个 chunk，需要扣住尾巴再还原。"""

    def __init__(self, sanitizer: Sanitizer) -> None:
        self.san = sanitizer
        self.buf = ""

    def feed(self, chunk: str) -> str:
        if not self.san.restore_enabled or not self.san.map:
            return chunk

        self.buf += chunk
        cut = len(self.buf)

        # 尾巴上若是半个占位符（有 [[ 但还没等到 ]]），先扣住
        m = OPEN_RE.search(self.buf)
        if m:
            cut = m.start()
        # 尾巴只剩一个 '['，可能是下一个占位符的开头
        elif self.buf.endswith("["):
            cut = len(self.buf) - 1

        out, self.buf = self.buf[:cut], self.buf[cut:]
        return self.san.restore(out)

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return self.san.restore(out)


if __name__ == "__main__":
    demo = (
        "key=AQ.Ab8RN6JkLmNoPqRsTuVwXyZ0123456789abcd\n"
        "DB_PASSWORD=Passw0rd!2026\n"
        "postgres://admin:hunter2@10.0.0.5:5432/prod\n"
        "mail me at alice.wang@example.com, tel 13800138000\n"
        "Authorization: Bearer abcdefghijklmnop123456\n"
        "card 4111 1111 1111 1111  id 110101199003077777\n"
        "普通代码不应被改：def get_user(uid): return uid\n"
    )
    s = Sanitizer(network=True)
    masked = s.scrub(demo)
    print(masked)
    print("-" * 60)
    back = s.restore(masked)
    print("可逆:", back == demo, s.counts)

    # 流式拆包测试：把占位符硬切成碎片
    s2 = Sanitizer()
    s2.scrub("token=ABCDEFGH1234567890")
    st = StreamRestorer(s2)
    pieces = ["token=", "[[SEC", "RET_", "1]]", " 结束"]
    out = "".join(st.feed(p) for p in pieces) + st.flush()
    print("流式还原:", out)
