# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""两条线的架构呈现：builder 并列两张图、main 常驻双服务。

只钉架构，不为 CLI 分发 / 可选参数这类胶水写测试；单测适配代码本身，而非让主链路
为测试开注入口。用户线执行见 test_graph.py，后台循环机制见 test_hotword_loop.py。
"""

from __future__ import annotations

import threading

from src.app import main as app_main
from src.app.graph import builder, hotword_graph
from src.app.tools.adapters.terms import hotword_loop as hloop


def test_builder_exposes_two_distinct_graphs():
    """装配层并列两张图：各自独立编译、互不嵌套，后台图就是叶子模块那张。"""
    user = {str(n) for n in builder.get_graph().get_graph().nodes}
    bg = {str(n) for n in builder.get_hotword_graph().get_graph().nodes}
    assert "classify_intent" in user and "fetch_one" not in user
    assert {"fetch_one", "analyze_one"} <= bg and "classify_intent" not in bg
    assert builder.get_graph() is not builder.get_hotword_graph()
    assert builder.get_hotword_graph is hotword_graph.get_hotword_graph


def test_background_line_uses_leaf_graph_without_deferred_import():
    """后台线顶层静态取叶子模块的图：无惰性边界、无 shim。"""
    from src.app.tools.adapters.terms import hotword_batch as hb

    assert not hasattr(hloop, "get_hotword_loop")   # shim 已删
    assert not hasattr(hb, "_hotword_graph")        # 惰性取图边界已删
    assert hb.get_hotword_graph is hotword_graph.get_hotword_graph


def test_serve_starts_both_lines(monkeypatch):
    """serve() 同一进程拉起 uvicorn（用户线）与后台守护线程（后台线）。"""
    seen, bg_ran = {}, threading.Event()

    def fake_uvicorn(*a, **kw):
        seen["http"] = True
        bg_ran.wait(timeout=2)   # 模拟服务常驻期间后台线在跑

    def fake_background(*a, **kw):
        seen["background"] = True
        bg_ran.set()

    monkeypatch.setattr(app_main.uvicorn, "run", fake_uvicorn)
    monkeypatch.setattr(app_main, "run_background", fake_background)
    monkeypatch.setattr(app_main.settings, "background_interval_sec", 3600)

    app_main.serve()
    assert seen == {"http": True, "background": True}
