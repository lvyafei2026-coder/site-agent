# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""单轮流水线：图装配、执行、路由分支、prompt 版本。"""

from src.app.enums import GraphNode, RouteTag
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


def test_review_metrics_runs_llm_analysis(graph, fake, monkeypatch):
    """review_metrics 取数后调用 LLM 分析各站表现，结论并进 output 报告。"""
    import src.app.services.metrics_service as metrics_mod

    captured = {}
    def fake_complete(system_prompt, text, **_kwargs):
        captured["system"] = system_prompt
        captured["data"] = text
        return "## 表现较好的站点\n1. **music.example** — page_view 高且 outbound 占比健康"
    monkeypatch.setattr(metrics_mod, "complete_text", fake_complete)

    r = graph.invoke({"input_text": "哪些站表现好", "intent": "review_metrics",
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-rm"}})
    assert captured, "review_metrics 应触发 metrics_service.analyze"
    # 提示词来自 D1 prompts 表（桩由 conftest 注入），且数据里含各站统计
    assert "review_metrics" in captured["system"] or "表现较好" in captured["system"]
    assert "music.example" in captured["data"]
    assert r["metrics_analysis"]
    assert "music.example" in r["output"]


def test_review_metrics_skips_analysis_without_key(graph, fake, monkeypatch):
    """无 LLM key 时 analyze 返回空串，不写 metrics_analysis，但整轮照常成功。"""
    import src.app.services.metrics_service as metrics_mod
    monkeypatch.setattr(metrics_mod.metrics_service, "analyze", lambda *a, **k: "")
    r = graph.invoke({"input_text": "复盘流量", "intent": "review_metrics",
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-rm-nokey"}})
    assert "metrics_analysis" not in r
    assert "status=success" in r["output"]


def test_word_plan_page_runs_llm_plan(graph, fake, monkeypatch):
    """word_plan_page 三份取数后调用 LLM 排产，方案并进 output 报告。"""
    import src.app.services.planning_service as planning_mod

    captured = {}
    def fake_complete(system_prompt, text, **_kwargs):
        captured["system"] = system_prompt
        captured["data"] = text
        return "## 本轮排产方案\n1. **music** ← free invoice calculator（待开发池第 1 名）\n排除：无"
    monkeypatch.setattr(planning_mod, "complete_text", fake_complete)

    r = graph.invoke({"input_text": "给待开发词排产", "intent": "word_plan_page",
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-wcp"}})
    assert set(r["tool_results"]) == {"get_hotwords", "get_page_inventory", "get_reflections"}
    assert captured, "word_plan_page 应触发 planning_service.plan"
    # 提示词来自 D1 prompts 表（桩由 conftest 注入），数据里带待开发词
    assert "word_plan_page" in captured["system"] or "排产" in captured["system"]
    assert "invoice" in captured["data"]
    assert r["planning_analysis"]
    assert "排产" in r["output"]


def test_word_plan_page_skips_plan_without_key(graph, fake, monkeypatch):
    """无 LLM key 时 plan 返回空串，不写 planning_analysis，整轮照常成功。"""
    import src.app.services.planning_service as planning_mod
    monkeypatch.setattr(planning_mod.planning_service, "plan", lambda *a, **k: "")
    r = graph.invoke({"input_text": "排产", "intent": "word_plan_page",
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-wcp-nokey"}})
    assert "planning_analysis" not in r
    assert "status=success" in r["output"]


def test_explicit_tools_override_intent(graph, fake):
    r = graph.invoke({"input_text": "hello", "intent": "general",
                      "requested_tools": ["get_latest_plan"],
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-2"}})
    assert list(r["tool_results"]) == ["get_latest_plan"]


def test_route_branches():
    assert route_after_validation({"validation": {"is_valid": True}}) == RouteTag.COMPLETE
    assert route_after_validation({"validation": {}, "retry_count": 0}) == RouteTag.RETRY
    assert route_after_validation({"validation": {}, "retry_count": 99}) == RouteTag.ERROR
    assert EDGE_MAP[RouteTag.COMPLETE] == GraphNode.OUTPUT
    assert EDGE_MAP[RouteTag.RETRY] == GraphNode.PROCESSING


def test_route_after_processing_short_circuits_on_fatal():
    from src.app.graph.edges import route_after_processing

    # 硬失败 → 直接 error，跳过校验
    assert route_after_processing({"processing_fatal": True}) == RouteTag.ERROR
    # 软失败（无该字段）/ 正常 → 进入校验
    assert route_after_processing({}) == RouteTag.VALIDATE
    assert route_after_processing({"processing_fatal": False}) == RouteTag.VALIDATE


def test_processing_llm_down_terminates_without_validate(graph, fake, monkeypatch):
    """LLM 已配置但调用挂了（明确非网络失败） → processing 置 fatal → 直接 handle_error，
    不进 validate、不重试。"""
    import src.app.services.extraction_service as ext_mod

    monkeypatch.setattr(ext_mod.extraction_service, "failed", True)
    monkeypatch.setattr(ext_mod.extraction_service, "extract", lambda *a, **k: {})

    r = graph.invoke(
        {"input_text": "看看新热词", "intent": "word_plan_page",
         "retry_count": 0, "messages": [], "tool_results": {}},
        config={"configurable": {"thread_id": "t-fatal-llm"}},
    )
    assert r.get("error"), "应短路到 handle_error 并带 error"
    assert r["output"].startswith("ERROR:")
    assert "validation" not in r, "硬失败不应再进 validate_result"
    assert r["retry_count"] == 1, "硬失败不重试，processing 只跑一次"


def test_processing_llm_network_timeout_goes_to_validate(graph, fake, monkeypatch):
    """LLM 是网络超时（retryable）→ 不短路，交 validate 走重试逻辑，不进 ERROR。"""
    import src.app.services.extraction_service as ext_mod

    monkeypatch.setattr(ext_mod.extraction_service, "failed", True)
    monkeypatch.setattr(ext_mod.extraction_service, "retryable", True)
    monkeypatch.setattr(ext_mod.extraction_service, "extract", lambda *a, **k: {})

    r = graph.invoke(
        {"input_text": "看看新热词", "intent": "word_plan_page",
         "retry_count": 0, "messages": [], "tool_results": {}},
        config={"configurable": {"thread_id": "t-llm-timeout"}},
    )
    assert "validation" in r, "网络超时不应短路，应进 validate"
    assert not r.get("processing_fatal"), "网络超时不置 fatal"


def test_processing_tool_exception_terminates(graph, fake, monkeypatch):
    """工具执行抛未捕获异常（明确非网络）→ processing 置 fatal → 直接 handle_error。"""
    import src.app.services.tool_service as ts_mod

    def boom(*_a, **_k):
        raise RuntimeError("radar 客户端炸了")

    monkeypatch.setattr(ts_mod.tool_service, "execute_for_intent", boom)

    r = graph.invoke(
        {"input_text": "复盘流量", "intent": "review_metrics",
         "retry_count": 0, "messages": [], "tool_results": {}},
        config={"configurable": {"thread_id": "t-fatal-tool"}},
    )
    assert r.get("error")
    assert "validation" not in r
    assert r["retry_count"] == 1


def test_processing_clear_tool_failure_short_circuits(graph, fake, monkeypatch):
    """工具返回明确非网络失败（retryable=False）→ processing 短路到 ERROR，不进 validate。"""
    from src.app.services.tool_service import tool_service

    def fake_exec(*_a, **_k):
        return {"get_click_stats": {"tool": "get_click_stats", "ok": False,
                                   "error": "auth 401", "retryable": False}}

    monkeypatch.setattr(tool_service, "execute_for_intent", fake_exec)

    r = graph.invoke(
        {"input_text": "复盘流量", "intent": "review_metrics",
         "retry_count": 0, "messages": [], "tool_results": {}},
        config={"configurable": {"thread_id": "t-clear-tool"}},
    )
    assert "validation" not in r, "明确非网络失败不应进 validate"
    assert r.get("error")
    assert r["retry_count"] == 1


def test_processing_network_tool_timeout_retries(graph, fake, monkeypatch):
    """工具返回网络超时（retryable=True）→ 不短路，交 validate 走重试逻辑。"""
    from src.app.services.tool_service import tool_service

    def fake_exec(*_a, **_k):
        return {"get_click_stats": {"tool": "get_click_stats", "ok": False,
                                   "error": "connect timeout", "retryable": True}}

    monkeypatch.setattr(tool_service, "execute_for_intent", fake_exec)

    r = graph.invoke(
        {"input_text": "复盘流量", "intent": "review_metrics",
         "retry_count": 0, "messages": [], "tool_results": {}},
        config={"configurable": {"thread_id": "t-net-tool"}},
    )
    assert "validation" in r, "网络超时不应短路，应进 validate"
    assert not r.get("processing_fatal")


def test_prompt_version_fallback_and_explicit():
    reg = PromptRegistry()
    assert reg.resolve_version("intent") == "v1"
    # 不存在的版本回退默认 v1；显式指定直读 v2（文案由 D1 桩提供，只验版本选择逻辑）
    assert reg.load("intent", "v99") == reg.load("intent", "v1")
    assert reg.load("intent", "v2") == "意图分类 system 桩 v2"


def test_workflow_result_shape():
    from src.app.models.workflow import WorkflowResult
    r = WorkflowResult.from_state({"intent": "general", "confidence": 0.7,
                                   "validation": {"is_valid": True, "reason": "ok"},
                                   "tool_results": {"x": {"ok": False, "error": "e"}}})
    assert r.status == "success" and r.tool_errors == ["x: e"]
    assert "tools=1" in r.render()
