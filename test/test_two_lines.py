# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""两条线的架构呈现：graph 统一导出两张图、入口只依赖 graph、serve 常驻双服务。

只钉架构，不为胶水写测试；单测适配代码本身，而非让主链路为测试开注入口。
用户线执行见 test_graph.py，后台循环机制见 test_hotword_loop.py。
"""

from __future__ import annotations

import threading

from src.app import main as app_main
from src.app import graph
from src.app import graph
from src.app.graph import hotword_graph, user_graph


def test_graph_package_exposes_two_distinct_graphs():
    """编排层并列两张对称命名的图：各自独立编译、互不嵌套。"""
    user = {str(n) for n in graph.get_user_graph().get_graph().nodes}
    bg = {str(n) for n in graph.get_hotword_graph().get_graph().nodes}
    assert "classify_intent" in user and "fetch_one" not in user
    assert {"fetch_one", "analyze_one"} <= bg and "classify_intent" not in bg
    assert graph.get_user_graph() is not graph.get_hotword_graph()
    # 对称命名 + 统一门面：两图函数同处 graph 包，无 builder 中间层
    assert graph.get_user_graph is user_graph.get_user_graph
    assert graph.get_hotword_graph is hotword_graph.get_hotword_graph


def test_entry_depends_only_on_graph():
    """入口只从 graph 取两条线的驱动，不穿透 services / tools / adapters。"""
    assert app_main.get_user_graph is graph.get_user_graph
    assert app_main.run_hotwords is graph.run_hotwords


def test_hotword_graph_owns_run_without_shim():
    """运行入口与图装配同处 hotword_graph：无 batch 中间层、无惰性取图、无函数内 import。"""
    assert not hasattr(hotword_graph, "_hotword_graph")   # 惰性取图边界已删
    assert callable(hotword_graph.run_hotwords)
    assert hotword_graph.get_hotword_graph is graph.get_hotword_graph


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
