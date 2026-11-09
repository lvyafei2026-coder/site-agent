# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""单轮流水线：图装配、执行、路由分支、prompt 版本。"""

from src.app.graph.constants import Names, Tags
from src.app.graph.edges import EDGE_MAP, route_after_validation
from src.app.prompts.registry import PromptRegistry


def test_graph_nodes(graph):
    nodes = set(graph.get_graph().nodes)
    assert {"classify_intent", "run_processing", "validate_result",
            "build_output", "handle_error"} <= nodes


def test_one_cycle_runs_to_output(graph, fake):
    r = graph.invoke({"input_text": "复盘点击与搜索表现", "intent": "review_metrics",
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-1"}})
    assert r["intent"] == "review_metrics"
    assert set(r["tool_results"]) == {"get_click_stats", "get_search_analytics", "get_page_inventory"}
    assert all(v["ok"] for v in r["tool_results"].values())
    assert "status=success" in r["output"]
    assert r.get("error") in (None, "")


def test_explicit_tools_override_intent(graph, fake):
    r = graph.invoke({"input_text": "hello", "intent": "general",
                      "requested_tools": ["get_latest_plan"],
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-2"}})
    assert list(r["tool_results"]) == ["get_latest_plan"]


def test_route_branches():
    assert route_after_validation({"validation": {"is_valid": True}}) == Tags.COMPLETE
    assert route_after_validation({"validation": {}, "retry_count": 0}) == Tags.RETRY
    assert route_after_validation({"validation": {}, "retry_count": 99}) == Tags.ERROR
    assert EDGE_MAP[Tags.COMPLETE] == Names.OUTPUT
    assert EDGE_MAP[Tags.RETRY] == Names.PROCESSING


def test_prompt_version_fallback_and_explicit():
    reg = PromptRegistry()
    assert reg.resolve_version("intent") == "v1"
    assert "意图分类器" in reg.load("intent", "v99")   # 不存在的版本回退 v1
    assert "显式动词" in reg.load("intent", "v2")      # 显式指定版本直读


def test_workflow_result_shape():
    from src.app.models.workflow import WorkflowResult
    r = WorkflowResult.from_state({"intent": "general", "confidence": 0.7,
                                   "validation": {"is_valid": True, "reason": "ok"},
                                   "tool_results": {"x": {"ok": False, "error": "e"}}})
    assert r.status == "success" and r.tool_errors == ["x: e"]
    assert "tools=1" in r.render()
