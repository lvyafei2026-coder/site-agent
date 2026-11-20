# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""入口架构：两条业务线各自独立、入口分层干净，且没有后台线程。

    /run       → 用户线主图（graph/user_graph.py）
    /hotwords  → 热词循环图（graph/hotword_graph.py），前端触发，无后台定时

只钉架构，不为胶水写测试；单测适配代码本身，而非让主链路为测试开注入口。
"""

from __future__ import annotations

from src.app import main as app_main
from src.app import graph
from src.app.graph import hotword_graph, user_graph


def test_graph_package_exposes_two_distinct_graphs():
    """编排层并列两张对称命名的图：各自独立编译、互不嵌套。"""
    user = {str(n) for n in graph.get_user_graph().get_graph().nodes}
    hw = {str(n) for n in graph.get_hotword_graph().get_graph().nodes}
    assert "classify_intent" in user and "fetch_one" not in user
    assert {"fetch_one", "analyze_one"} <= hw and "classify_intent" not in hw
    assert graph.get_user_graph() is not graph.get_hotword_graph()
    assert graph.get_user_graph is user_graph.get_user_graph
    assert graph.get_hotword_graph is hotword_graph.get_hotword_graph


def test_entry_depends_only_on_graph():
    """入口只从 graph 取用户线图，不穿透 services / tools / adapters。"""
    assert app_main.get_user_graph is graph.get_user_graph


def test_no_background_thread():
    """后台线程已移除：热词分析由 /hotwords 前端触发，进程内不再有定时循环。"""
    assert not hasattr(app_main, "_background_loop")
    assert not hasattr(app_main, "run_background")
    assert not hasattr(app_main, "threading")


def test_hotword_graph_owns_run_without_shim():
    """运行入口与图装配同处 hotword_graph：无中间层、无惰性取图、无函数内 import。"""
    assert not hasattr(hotword_graph, "_hotword_graph")
    assert callable(hotword_graph.run_hotwords)
    assert hotword_graph.get_hotword_graph is graph.get_hotword_graph


def test_serve_starts_http_only(monkeypatch):
    """serve() 只拉起 uvicorn，没有守护线程。"""
    seen = {}

    def fake_uvicorn(*a, **kw):
        seen["http"] = True

    monkeypatch.setattr(app_main.uvicorn, "run", fake_uvicorn)
    app_main.serve()
    assert seen == {"http": True}
