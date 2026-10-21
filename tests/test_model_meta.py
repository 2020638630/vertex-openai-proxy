# -*- coding: utf-8 -*-
"""/v1/models 附带上下文元数据。"""

from __future__ import annotations

from fastapi.testclient import TestClient

import vertex_openai_proxy as vp


def test_list_models_includes_context_window():
    client = TestClient(vp.app)
    data = client.get("/v1/models").json()["data"]
    by_id = {m["id"]: m for m in data}

    # 核实过的模型带上下文字段
    assert by_id["gemini-2.5-pro"]["context_window"] == 1048576
    assert by_id["gemini-2.5-pro"]["max_output_tokens"] == 65536
    assert by_id["deepseek/deepseek-flash"]["context_window"] == 1048576
    assert by_id["deepseek/deepseek-flash"]["max_output_tokens"] == 393216


def test_unknown_model_has_no_fake_numbers():
    """未核实的模型不能编造上下文长度（gemini-4-argon / 3.8-flash 就是这种）。"""
    entry = vp._model_entry("gemini-4-argon", "google", 0)
    assert "context_window" not in entry
    assert entry["id"] == "gemini-4-argon"


def test_provider_models_are_exposed_with_prefix():
    entry = vp._model_entry("glm/glm-4.5-air", "glm", 0)
    assert entry["context_window"] == 131072
