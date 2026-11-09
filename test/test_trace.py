# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""图执行轨迹：/run 返回的 trace 要能说明「走了哪些节点、每步改了什么」。"""

from __future__ import annotations

import json

from src.app.api.routes import invoke_workflow

NODES = ("classify_intent", "run_processing", "validate_result", "build_output", "handle_error")


def test_trace_success_path(fake):
    r = invoke_workflow({"input_text": "看看新热词", "intent": "select_pages"})
    trace = r["trace"]

    assert trace["route"] == "complete"
    assert trace["retried"] is False
    assert [n["node"] for n in trace["nodes"]] == \
        ["classify_intent", "run_processing", "validate_result", "build_output"]
    assert trace["path"].startswith("classify_intent → run_processing")
    assert trace["totalMs"] >= 0
    assert all(n["node"] in NODES for n in trace["nodes"])


def test_trace_error_path_with_retry(fake, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("radar 挂了")

    # select_pages 挂三个只读工具，全部打挂才能逼出 handle_error
    for name in ("hotwords", "page_inventory", "reflections"):
        monkeypatch.setattr(fake, name, boom)
    r = invoke_workflow({"input_text": "看看新热词", "intent": "select_pages"})
    trace = r["trace"]

    assert trace["route"] == "error"
    assert trace["retried"] is True
    nodes = [n["node"] for n in trace["nodes"]]
    assert nodes[-1] == "handle_error"
    assert nodes.count("run_processing") == 2          # 首次 + 一次重试
    assert r["error"]


def test_trace_changed_fields_are_small(fake):
    """trace 只放摘要：不许把 tool_results 里的整份热词塞进来。"""
    r = invoke_workflow({"input_text": "看看新热词", "intent": "select_pages"})
    trace = r["trace"]

    assert json.dumps(trace, ensure_ascii=False).__len__() < 4000
    step = next(n for n in trace["nodes"] if n["node"] == "run_processing")
    assert "tools" in step["changed"]                  # 形如 "2/2 ok"
    assert "tool_results" not in step["changed"]


def test_trace_does_not_change_final_state(fake):
    """加 trace 不能动终态字段，老前端照旧吃 intent / validation / output。"""
    r = invoke_workflow({"input_text": "复盘流量", "intent": "review_metrics"})

    assert r["intent"] == "review_metrics"
    assert isinstance(r["validation"], dict)
    assert "output" in r and r["output"]
