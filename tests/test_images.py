# -*- coding: utf-8 -*-
"""图片块转换：base64 / 本地路径 / file:// / 远程 URL / 多种块类型。

客户端（ZCode 等）常把图片缓存到本地，只把路径塞进 image_url；
Vertex 只认 gs:// 与 https://，所以本地路径必须由代理读成字节。
"""

from __future__ import annotations

import base64

import vertex_openai_proxy as vp

# 1×1 透明 PNG，避免测试依赖 Pillow
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _parts(msg_content):
    _sys, contents = vp.build_contents([{"role": "user", "content": msg_content}])
    return [p for c in contents for p in (c.parts or [])]


def test_data_url_becomes_inline_data():
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    parts = _parts(
        [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": url}},
        ]
    )
    blobs = [p for p in parts if getattr(p, "inline_data", None)]
    assert blobs and blobs[0].inline_data.mime_type == "image/png"
    assert blobs[0].inline_data.data == PNG


def test_local_path_is_read_from_disk(tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    parts = _parts(
        [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": str(f)}},
        ]
    )
    blobs = [p for p in parts if getattr(p, "inline_data", None)]
    assert blobs, "本地路径应被读成 inline_data"
    assert blobs[0].inline_data.data == PNG


def test_file_scheme_is_supported(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(PNG)
    parts = _parts([{"type": "image_url", "image_url": {"url": f.as_uri()}}])
    blobs = [p for p in parts if getattr(p, "inline_data", None)]
    assert blobs and blobs[0].inline_data.mime_type == "image/jpeg"


def test_input_image_block_type_is_supported(tmp_path):
    f = tmp_path / "b.png"
    f.write_bytes(PNG)
    parts = _parts([{"type": "input_image", "image_url": {"url": str(f)}}])
    assert [p for p in parts if getattr(p, "inline_data", None)]

    # 也有客户端把 image_url 直接写成字符串
    parts2 = _parts([{"type": "input_image", "image_url": str(f)}])
    assert [p for p in parts2 if getattr(p, "inline_data", None)]


def test_remote_url_becomes_file_data():
    parts = _parts(
        [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]
    )
    files = [p for p in parts if getattr(p, "file_data", None)]
    assert files and files[0].file_data.file_uri == "https://example.com/a.png"


def test_unsupported_extension_is_skipped(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("not an image", encoding="utf-8")
    parts = _parts([{"type": "image_url", "image_url": {"url": str(f)}}])
    assert not [p for p in parts if getattr(p, "inline_data", None)]


def test_oversize_image_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(vp, "MAX_IMAGE_BYTES", 8)
    f = tmp_path / "big.png"
    f.write_bytes(PNG)
    parts = _parts([{"type": "image_url", "image_url": {"url": str(f)}}])
    assert not [p for p in parts if getattr(p, "inline_data", None)]


def test_missing_file_is_skipped(tmp_path):
    parts = _parts(
        [{"type": "image_url", "image_url": {"url": str(tmp_path / "none.png")}}]
    )
    assert not [p for p in parts if getattr(p, "inline_data", None)]
