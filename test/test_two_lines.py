# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""两条线在入口/装配层的可见性：builder 并列两张图、main 常驻双线 + 单跑入口。

不重复 test_graph.py（用户线执行）与 test_hotword_loop.py（后台线循环机制）的断言，
这里只钉「架构呈现」这件事：两张图都在 builder 装配、互不嵌套；入口同时拉起两条线
（serve），且各自可单跑调试。
"""

from __future__ import annotations

import threading

from src.app import main as app_main
from src.app.graph import builder, hotword_graph
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
    # builder 的后台图就是叶子模块装配的那张，no shim
    assert builder.get_hotword_graph is hotword_graph.get_hotword_graph


def test_background_line_uses_leaf_graph_without_deferred_import():
    """后台线顶层静态取叶子模块的图：无惰性边界、无 shim。"""
    from src.app.tools.adapters.terms import hotword_batch as hb

    assert not hasattr(hloop, "get_hotword_loop")   # shim 已删
    assert not hasattr(hb, "_hotword_graph")        # 惰性取图边界已删
    assert hb.get_hotword_graph is hotword_graph.get_hotword_graph  # 同一函数对象
    assert hb.get_hotword_graph() is builder.get_hotword_graph()    # 同一图实例


def test_main_exposes_entry_lines():
    """入口层：用户线 run()、后台线 run_background()、常驻 serve()、分发 main()。"""
    assert callable(app_main.run)
    assert callable(app_main.run_background)
    assert callable(app_main.serve)
    assert callable(app_main.main)
    assert callable(app_main._background_loop)


def test_cli_dispatch_selects_background_line(monkeypatch):
    """main(['background']) 只派发到后台线，不碰用户线。"""
    called = []
    monkeypatch.setattr(app_main, "run_background", lambda: called.append("bg") or {"ok": True})
    monkeypatch.setattr(app_main, "run", lambda: called.append("user") or {"ok": True})
    app_main.main(["background"])
    assert called == ["bg"]


def test_cli_dispatch_user_line(monkeypatch):
    """main(['user']) 单跑用户线。"""
    called = []
    monkeypatch.setattr(app_main, "run_background", lambda: called.append("bg"))
    monkeypatch.setattr(app_main, "run", lambda: called.append("user") or {"ok": True})
    app_main.main(["user"])
    assert called == ["user"]


def test_cli_dispatch_defaults_to_serve(monkeypatch):
    """无参数时走常驻双服务（对外用户输入是默认形态，两条线一起起）。"""
    called = []
    monkeypatch.setattr(app_main, "serve", lambda: called.append("serve") or {"ok": True})
    app_main.main([])
    assert called == ["serve"]


def test_serve_starts_both_lines(monkeypatch):
    """serve() 同时拉起 uvicorn（用户线）与后台守护线程（后台线）。"""
    seen = {"http": 0, "bg": 0}

    monkeypatch.setattr(app_main.uvicorn, "run",
                        lambda api, **kw: seen.__setitem__("http", seen["http"] + 1))
    monkeypatch.setattr(app_main, "run_background",
                        lambda: seen.__setitem__("bg", seen["bg"] + 1))

    out = app_main.serve(host="127.0.0.1", port=8000, interval_sec=3600)
    assert seen["http"] == 1
    assert seen["bg"] >= 1            # 启动即跑一轮
    assert out["backgroundEnabled"] is True
    assert out["port"] == 8000


def test_serve_can_disable_background(monkeypatch):
    """interval_sec<=0 时只跑用户线，不拉后台线程。"""
    seen = {"http": 0, "bg": 0}
    monkeypatch.setattr(app_main.uvicorn, "run", lambda api, **kw: seen.__setitem__("http", 1))
    monkeypatch.setattr(app_main, "run_background",
                        lambda: seen.__setitem__("bg", seen["bg"] + 1))

    out = app_main.serve(host="127.0.0.1", port=8000, interval_sec=0)
    assert seen["http"] == 1
    assert seen["bg"] == 0
    assert out["backgroundEnabled"] is False


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


def test_background_loop_runs_until_stop(monkeypatch):
    """后台循环：启动即跑一轮；stop 置位后中断等待、不再追加。"""
    seen = {"n": 0}
    stop = threading.Event()

    def fake_bg():
        seen["n"] += 1
        stop.set()   # 第一轮就置位，验证 stop.wait 能中断、循环收口

    monkeypatch.setattr(app_main, "run_background", fake_bg)
    app_main._background_loop(stop, interval_sec=3600)
    assert seen["n"] == 1
