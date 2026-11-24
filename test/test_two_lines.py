# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""入口架构：两条业务线各自独立、入口分层干净，且没有后台线程。

    /run       → 常规线主图（graph/normal_graph.py）
    /hotwords  → 热词循环图（graph/hotword_graph.py），前端触发，无后台定时

只钉架构，不为胶水写测试；单测适配代码本身，而非让主链路为测试开注入口。
"""

from __future__ import annotations

from src.app import main as app_main
from src.app import graph
from src.app.graph import hotword_graph, normal_graph
from src.app.graph import state as graph_state
from src.app.observability.trace import run_traced


def test_graph_package_exposes_two_distinct_graphs():
    """编排层并列两张对称命名的图：各自独立编译、互不嵌套。"""
    normal = {str(n) for n in graph.get_normal_graph().get_graph().nodes}
    hw = {str(n) for n in graph.get_hotword_graph().get_graph().nodes}
    assert "classify_intent" in normal and "fetch_one" not in normal
    assert {"fetch_one", "analyze_word"} <= hw and "classify_intent" not in hw
    assert graph.get_normal_graph() is not graph.get_hotword_graph()
    assert graph.get_normal_graph is normal_graph.get_normal_graph
    assert graph.get_hotword_graph is hotword_graph.get_hotword_graph


def test_two_lines_share_one_mechanism():
    """机制同构：build/get/run 三件套对称、state 工厂并列、共用同一个 run_traced 驱动器。"""
    assert normal_graph.run_traced is run_traced
    assert hotword_graph.run_traced is run_traced
    assert callable(graph_state.initial_workflow_state)
    assert callable(graph_state.initial_hotword_state)
    assert callable(normal_graph.build_normal_graph)
    assert callable(hotword_graph.build_hotword_graph)
    assert callable(normal_graph.run_workflow) and callable(hotword_graph.run_hotwords)


def test_entry_has_single_run_path():
    """常规线只走 `POST /run`：main 不再自带「单跑一轮」的第二个入口。"""
    assert not hasattr(app_main, "run")      # langgraph.json 的 CLI 入口已删
    assert callable(app_main.serve)


def test_no_background_thread():
    """后台线程已移除：候选池分析由 /hotwords 前端触发，进程内不再有定时循环。"""
    assert not hasattr(app_main, "_background_loop")
    assert not hasattr(app_main, "run_background")
    assert not hasattr(app_main, "threading")


def test_hotword_graph_owns_run_without_shim():
    """运行入口与图装配同处一个文件：无中间层、无惰性取图、无函数内 import。"""
    assert not hasattr(hotword_graph, "_hotword_graph")
    assert not hasattr(normal_graph, "_normal_graph")
    assert callable(hotword_graph.run_hotwords)
    assert hotword_graph.get_hotword_graph is graph.get_hotword_graph
    assert normal_graph.get_normal_graph is graph.get_normal_graph


def test_serve_starts_http_only(monkeypatch):
    """serve() 只拉起 uvicorn，没有守护线程。"""
    seen = {}

    def fake_uvicorn(*a, **kw):
        seen["http"] = True

    monkeypatch.setattr(app_main.uvicorn, "run", fake_uvicorn)
    app_main.serve()
    assert seen == {"http": True}
