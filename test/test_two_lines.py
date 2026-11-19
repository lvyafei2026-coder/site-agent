# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""两条线在入口/装配层的可见性：builder 并列两张图、main 两个入口线。

不重复 test_graph.py（用户线执行）与 test_hotword_loop.py（后台线循环机制）的断言，
这里只钉「架构呈现」这件事：两张图都在 builder 装配、互不嵌套；两个入口各自可达，
且后台入口确实转发到热词门面。
"""

from __future__ import annotations

from src.app import main as app_main
from src.app.graph import builder
from src.app.tools.adapters.terms import hotword_loop as hloop


def test_builder_exposes_both_graphs():
    """装配层并列两张图：用户线与后台线，各自独立编译。"""
    assert callable(builder.get_graph)
    assert callable(builder.get_hotword_graph)
    user = {str(n) for n in builder.get_graph().get_graph().nodes}
    bg = {str(n) for n in builder.get_hotword_graph().get_graph().nodes}
    # 用户线是意图流水线节点；后台线是逐词循环节点；互不混淆
    assert "classify_intent" in user
    assert {"fetch_one", "analyze_one"} <= bg
    assert "classify_intent" not in bg
    assert "fetch_one" not in user


def test_graphs_are_distinct_not_nested():
    """两图是并列关系，不是一张嵌进另一张（不同图对象）。"""
    assert builder.get_graph() is not builder.get_hotword_graph()


def test_compat_shim_points_to_background_graph():
    """老引用 hotword_loop.get_hotword_loop 转发到 builder 的后台线图，契约不破。"""
    assert hloop.get_hotword_loop() is builder.get_hotword_graph()


def test_main_exposes_both_entry_lines():
    """入口层并列两条线：用户线 run() 与后台线 run_background()。"""
    assert callable(app_main.run)
    assert callable(app_main.run_background)
    assert callable(app_main.main)


def test_cli_dispatch_selects_background_line(monkeypatch):
    """main(['background']) 只派发到后台线，不碰用户线。"""
    called = []
    monkeypatch.setattr(app_main, "run_background", lambda: called.append("bg") or {"ok": True})
    monkeypatch.setattr(app_main, "run", lambda: called.append("user") or {"ok": True})
    app_main.main(["background"])
    assert called == ["bg"]


def test_cli_dispatch_defaults_to_user_line(monkeypatch):
    """无参数时走用户线（对外用户输入是默认线）。"""
    called = []
    monkeypatch.setattr(app_main, "run_background", lambda: called.append("bg"))
    monkeypatch.setattr(app_main, "run", lambda: called.append("user") or {"ok": True})
    app_main.main([])
    assert called == ["user"]


def test_background_line_forwards_to_facade(monkeypatch):
    """后台入口把 limit / prompt_version 透传给热词门面 run_batch。"""
    from src.app.tools.adapters.terms import hotwords as hw

    seen = {}
    def fake_run_batch(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "analyzed": 3, "failed": 0}

    monkeypatch.setattr(hw, "run_batch", fake_run_batch)
    out = app_main.run_background(limit=7, prompt_version="v9")
    assert out["analyzed"] == 3
    assert seen == {"limit": 7, "prompt_version": "v9"}
