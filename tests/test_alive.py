# -*- coding: utf-8 -*-
"""心跳文件：区分「代理没运行」和「运行了但 0 调用」。"""

from __future__ import annotations

import os

import usage as U


def test_alive_marker_written_and_reported(tmp_path):
    u = U.Usage(data_dir=str(tmp_path))
    assert u.stats(days=1)["daily"][0]["alive"] is False, "没心跳时应为 False"

    u.touch_alive(force=True)
    assert os.path.exists(u._alive_path()), "心跳文件应落盘"

    day = u.stats(days=1)["daily"][0]
    assert day["alive"] is True
    assert u.stats(days=1)["today"]["alive"] is True


def test_touch_alive_is_throttled(tmp_path, monkeypatch):
    u = U.Usage(data_dir=str(tmp_path))
    u.touch_alive(force=True)
    first = os.path.getmtime(u._alive_path())

    # 未超时（60s）时不应重写文件
    monkeypatch.setattr(u, "_last_alive", __import__("time").time())
    u.touch_alive()
    assert os.path.getmtime(u._alive_path()) == first


def test_zero_traffic_differs_from_no_run(tmp_path):
    """0 调用但有心跳 → alive=True；从未运行 → alive=False。"""
    u = U.Usage(data_dir=str(tmp_path))
    u.touch_alive(force=True)  # 只有心跳，没有任何请求记录
    row = u.stats(days=1)["daily"][0]
    assert row["requests"] == 0 and row["alive"] is True
