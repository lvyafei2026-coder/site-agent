# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LangSmith 层级与工具可见性回归。

两轮背景（2026-11-13）：
  ① ThreadPoolExecutor 的 worker 线程不继承主线程的 contextvars，LangSmith 的父 run
     存在 contextvar 里，于是 20 个 wrap_openai 调用全变成孤立根 trace（Traces 页面
     「连不起来」）。这里用真实 SDK（只把 post/patch 换成内存收集）验证显式 parent
     能钉死父子链，且 LLM run 真的挂在对应词 span 下。
  ② 只有 wrap_openai 会自动打点，SERP / Trends / KV 读写 / 26 个 RuntimeTool 在
     Smith 上完全看不见；源挂了只剩一段 note，排查时不知道调用发生过没有。这里验证
     每个工具调用都有 `run_type=tool` 的 span、带真实出入参，失败会标红。
"""

from __future__ import annotations

import json

import pytest
from langsmith import client as ls_client, run_helpers, schemas as ls_schemas
from langsmith import trace as ls_trace, traceable, utils
from langsmith.run_trees import RunTree

from src.app.config import settings
from src.app.tools.adapters.sources import hotwords as hw
from src.app.tools.registry import tool_registry


@pytest.fixture
def traced_runs(monkeypatch):
    """打开追踪但不出网：post 收集 RunTree，patch 空转。"""
    monkeypatch.setattr(settings, "langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "lsv2_pt_fake")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_fake")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    # SDK 的 enabled 判定有 env lru_cache，测试间直接钉死，避免互相污染
    utils.get_env_var.cache_clear()
    monkeypatch.setattr(utils, "tracing_is_enabled", lambda ctx=None: True)

    # Client.info 默认会同步 GET /info；桩掉它，测试既不触网也不刷 SDK 警告
    monkeypatch.setattr(ls_client.Client, "info",
                        property(lambda self: ls_schemas.LangSmithInfo()))
    runs: list[RunTree] = []
    monkeypatch.setattr(RunTree, "post", lambda self: runs.append(self))
    monkeypatch.setattr(RunTree, "patch", lambda self: None)
    yield runs
    utils.get_env_var.cache_clear()


def _seed(fake_kv, monkeypatch, count=6, llm=None):
    """离线铺 6 个候选词：SERP / Trends / LLM 全部替身，不碰任何外部服务。"""
    monkeypatch.setattr(hw, "SERP_GAP_SEC", 0)
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(count)])
    monkeypatch.setattr(hw.scrape_serp, "fetch_serp",
                        lambda term, session=None, limit=10: {
                            "engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest",
                        lambda term, session=None, geo="": {
                            "points": [{"d": "2026-01-01", "v": 50}],
                            "summary": {"avg": 50}, "attempts": []})
    monkeypatch.setattr(hw, "_llm", llm or (lambda messages: (
        "7) 结论：观望，等长尾\n8) 信号标签：工具查询", "fake")))


@traceable(run_type="llm", name="ChatOpenAI")
def _fake_openai_call(term: str) -> dict:
    """模拟 wrap_openai 装饰过的 create：验证它把父 run 认成词 span。"""
    return {"choices": [{"message": {"content": "ok"}}]}


def test_batch_tree_is_nested_under_graph_root(fake_kv, monkeypatch, traced_runs):
    """一条根展开看完 6 个词：批次挂 graph 根、每词挂批次、LLM 挂对应词。"""
    seen_in_llm: list[tuple[str, str, str]] = []

    def llm(messages):
        current = run_helpers.get_current_run_tree()
        seen_in_llm.append((current.name, str(current.id), str(current.parent_run_id))
                           if current else ("", "", ""))
        term = messages[-1]["content"].split("关键词：")[1].split("\n")[0]
        _fake_openai_call(term)
        return "7) 结论：观望，等长尾\n8) 信号标签：工具查询", "fake"

    _seed(fake_kv, monkeypatch, count=6, llm=llm)

    with ls_trace("site-agent:analyze_hotwords") as root:
        out = hw.run_batch(limit=6, kv=fake_kv)

    batch = next(r for r in traced_runs if r.name == hw.BATCH_TRACE_NAME)
    words = [r for r in traced_runs if r.name.startswith(hw.WORD_TRACE_PREFIX)]
    llms = [r for r in traced_runs if r.name == "ChatOpenAI"]

    assert str(batch.parent_run_id) == str(root.id)
    assert len(words) == 6 and len(llms) == 6
    assert {str(w.parent_run_id) for w in words} == {str(batch.id)}
    # 每个 LLM run 的父都是某个词 span，不是批次、更不是新的根
    assert all(any(str(r.parent_run_id) == str(w.id) for w in words) for r in llms)
    # worker 线程里 get_current_run_tree() 拿到的正是本词的 span
    assert len(seen_in_llm) == 6
    assert all(name.startswith(hw.WORD_TRACE_PREFIX) for name, _, _ in seen_in_llm)
    assert {parent for _, _, parent in seen_in_llm} == {str(batch.id)}

    assert out["analyzed"] == 6
    assert out["traceRun"]["batchId"].startswith("hotwords-")
    assert out["traceRun"]["runId"] == str(batch.id)
    # 整批共用 thread_id，Threads 视图也聚成一条而不是 6 条
    assert {r.extra["metadata"]["thread_id"] for r in [batch, *words]} == {
        out["traceRun"]["batchId"]}
    assert len({r.extra["metadata"]["word_id"] for r in words}) == 6
    # worker 线程没有把 contextvar 泄漏回主线程
    assert run_helpers.get_current_run_tree() is None


def test_word_span_marks_business_error_without_breaking_others(fake_kv, monkeypatch,
                                                                traced_runs):
    """某词 LLM 抛异常：该词 span 标红写 error，其余词照常完成并挂在同一批次下。"""
    def llm(messages):
        term = messages[-1]["content"].split("关键词：")[1].split("\n")[0]
        if term.endswith("2"):
            raise RuntimeError("LLM 连接超时")
        return "7) 结论：观望，等长尾\n8) 信号标签：工具查询", "fake"

    _seed(fake_kv, monkeypatch, count=5, llm=llm)

    with ls_trace("site-agent:analyze_hotwords") as root:
        out = hw.run_batch(limit=5, kv=fake_kv)

    batch = next(r for r in traced_runs if r.name == hw.BATCH_TRACE_NAME)
    words = [r for r in traced_runs if r.name.startswith(hw.WORD_TRACE_PREFIX)]
    assert str(batch.parent_run_id) == str(root.id)
    assert len(words) == 5
    assert {str(w.parent_run_id) for w in words} == {str(batch.id)}
    failed = [w for w in words if w.error]
    assert len(failed) == 1 and "LLM 连接超时" in failed[0].error
    assert out["analyzed"] == 4 and out["failed"] == 1
    assert batch.outputs["analyzed"] == 4


def test_batch_nests_under_real_langgraph_node(fake_kv, monkeypatch, traced_runs):
    """真实 LangGraph 节点里调用（不是手工造的根）：批次 span 要挂在节点 run 下。"""
    from typing import TypedDict

    from langchain_core.tracers.langchain import LangChainTracer
    from langgraph.graph import END, START, StateGraph

    # langchain_core.tracers.schemas.Run 就是 RunTree 的别名，直接桩它会把上面
    # 收集 span 的 post 覆盖掉；改为桩 tracer 的上报入口，保留 RunTree.post 收集。
    monkeypatch.setattr(LangChainTracer, "_persist_run_single", lambda self, run: None)
    _seed(fake_kv, monkeypatch, count=3)

    node_run_ids: list[str] = []

    class S(TypedDict):
        out: dict

    def node(state: S) -> dict:
        current = run_helpers.get_current_run_tree()
        node_run_ids.append(str(current.id) if current else "")
        return {"out": hw.run_batch(limit=3, kv=fake_kv)}

    graph = (StateGraph(S).add_node("analyze", node)
             .add_edge(START, "analyze").add_edge("analyze", END).compile())
    result = graph.invoke({"out": {}},
                          config={"run_name": "site-agent:analyze_hotwords"})

    batch = next(r for r in traced_runs if r.name == hw.BATCH_TRACE_NAME)
    words = [r for r in traced_runs if r.name.startswith(hw.WORD_TRACE_PREFIX)]
    assert node_run_ids and node_run_ids[0]
    assert str(batch.parent_run_id) == node_run_ids[0]
    assert len(words) == 3
    assert {str(w.parent_run_id) for w in words} == {str(batch.id)}
    assert result["out"]["analyzed"] == 3


def test_early_return_still_has_batch_span_and_link(monkeypatch, traced_runs):
    """KV 读挂了提前返回：批次 span 照样记录 error，返回照样带根 run 回链。"""
    class BoomKV:
        def get_json(self, *a, **k):
            raise RuntimeError("unreachable")

    with ls_trace("site-agent:analyze_hotwords") as root:
        out = hw.run_batch(limit=5, kv=BoomKV())

    batch = next(r for r in traced_runs if r.name == hw.BATCH_TRACE_NAME)
    assert str(batch.parent_run_id) == str(root.id)
    assert "候选池读取失败" in out["error"]
    assert out["traceRun"]["runId"] == str(batch.id)
    assert batch.outputs["error"].startswith("候选池读取失败")


def test_tracing_off_has_no_spans_and_plain_result(fake_kv, monkeypatch):
    """关闭追踪时零开销：正常出结果，不生成 traceRun，也不 import langsmith 运行时。"""
    monkeypatch.setattr(settings, "langsmith_tracing", False)
    monkeypatch.setattr(settings, "langsmith_api_key", "")
    _seed(fake_kv, monkeypatch, count=3)

    out = hw.run_batch(limit=3, kv=fake_kv)
    assert out["analyzed"] == 3
    assert "traceRun" not in out
    assert run_helpers.get_current_run_tree() is None


# ---------------------------------------------------------------------------
# 工具可见性：SERP / Trends / KV / 26 个 RuntimeTool 都要在 Smith 上看得到出入参
# ---------------------------------------------------------------------------
def test_batch_tool_spans_capture_serp_trend_kv_io(fake_kv, monkeypatch, traced_runs):
    """每词三条 tool span + 批次一条候选池读 span，父链与出入参都要对。"""
    def llm(messages):
        _fake_openai_call("x")
        return "7) 结论：不做，影视作品\n8) 信号标签：工具查询", "fake"

    _seed(fake_kv, monkeypatch, count=3, llm=llm)
    with ls_trace("site-agent:analyze_hotwords") as root:
        out = hw.run_batch(limit=3, kv=fake_kv)

    batch = next(r for r in traced_runs if r.name == hw.BATCH_TRACE_NAME)
    words = [r for r in traced_runs if r.name.startswith(hw.WORD_TRACE_PREFIX)]
    tools = [r for r in traced_runs if r.name.startswith(hw.TOOL_TRACE_PREFIX)]
    assert str(batch.parent_run_id) == str(root.id)
    assert len(words) == 3
    # 3 词 × (serp/trend/kv.write) + 1 条候选池读
    assert len(tools) == 10
    assert all(r.run_type == "tool" for r in tools)

    readers = [r for r in tools if r.name == "tool:kv.read-candidates"]
    assert len(readers) == 1 and str(readers[0].parent_run_id) == str(batch.id)
    assert readers[0].inputs["limit"] == 3
    assert readers[0].outputs["selected"] == 3
    assert len(readers[0].outputs["terms"]) == 3

    per_word = [r for r in tools if r is not readers[0]]
    word_ids = {str(w.id) for w in words}
    assert all(str(r.parent_run_id) in word_ids for r in per_word)
    for name in ("tool:serp", "tool:trend", "tool:kv.write"):
        assert sum(1 for r in per_word if r.name == name) == 3

    serp = next(r for r in per_word if r.name == "tool:serp")
    assert "term" in serp.inputs and serp.inputs["cacheKey"].startswith(hw.SERP_PREFIX)
    assert serp.outputs["fromCache"] is False
    assert "itemCount" in serp.outputs and "competition" in serp.outputs
    # 现抓后写了 serp:v1: 与 competition 两张表，明细里能看到每个 key 与成功与否
    serp_keys = {w["key"] for w in serp.outputs["kvWrites"]}
    assert any(k.startswith(hw.SERP_PREFIX) for k in serp_keys)
    assert hw.COMPETITION_KEY in serp_keys
    assert all(w["ok"] for w in serp.outputs["kvWrites"])

    trend = next(r for r in per_word if r.name == "tool:trend")
    assert trend.inputs["cacheKey"].startswith(hw.TREND_PREFIX)
    assert "pointCount" in trend.outputs and trend.outputs["kvWrites"]

    kvwrite = next(r for r in per_word if r.name == "tool:kv.write")
    assert kvwrite.inputs["aiCacheKey"].startswith(hw.AI_CACHE_PREFIX)
    assert kvwrite.inputs["verdict"] == "不做"
    assert kvwrite.outputs["textChars"] > 0 and kvwrite.outputs["textPreview"]
    assert kvwrite.outputs["status"] == "rejected"
    assert hw.AI_CACHE_PREFIX + hw.term_key(kvwrite.inputs["term"]) in {
        w["key"] for w in kvwrite.outputs["writes"]}
    assert kvwrite.outputs["writeErrors"] == []

    # LLM 仍挂在对应词 span 下，没有被新的 tool span 挤成孤根
    llms = [r for r in traced_runs if r.name == "ChatOpenAI"]
    assert len(llms) == 3
    assert all(str(r.parent_run_id) in word_ids for r in llms)
    assert out["analyzed"] == 3


def test_source_down_marks_tool_spans_red_and_batch_continues(fake_kv, monkeypatch,
                                                             traced_runs):
    """SERP/Trends 源整体挂掉：两个 tool span 标红带真因，批次不中止、降级提示可见。

    跑 8 个词跨两个 chunk：第一批撞上源故障并标红，第二批走「本批已跳过」分支，
    两种情形都要留痕（同一 chunk 内是并发的，不保证有跳过分支）。
    """
    _seed(fake_kv, monkeypatch, count=8)

    def serp_boom(term, session=None, limit=10):
        raise hw.SourceUnavailable("duckduckgo", "连不上 DuckDuckGo 出口", [])

    def trend_boom(term, session=None, geo=""):
        raise hw.SourceUnavailable("google-trends", "Google Trends 连接超时", [])

    monkeypatch.setattr(hw.scrape_serp, "fetch_serp", serp_boom)
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest", trend_boom)

    with ls_trace("site-agent:analyze_hotwords"):
        out = hw.run_batch(limit=8, kv=fake_kv)

    serp_spans = [r for r in traced_runs if r.name == "tool:serp"]
    trend_spans = [r for r in traced_runs if r.name == "tool:trend"]
    assert len(serp_spans) == 8 and len(trend_spans) == 8
    assert any(r.error and "SourceUnavailable" in r.error for r in serp_spans)
    assert any(r.error and "SourceUnavailable" in r.error for r in trend_spans)
    # 后续词跳过取数也要留一条 span，而不是在监控里凭空消失
    assert any(r.outputs.get("skipped") for r in serp_spans)
    assert any(r.outputs.get("skipped") for r in trend_spans)

    assert out["serpDisabled"] is True and out["trendsDisabled"] is True
    assert "SERP 源不可用" in out["note"] and "趋势源不可用" in out["note"]
    assert out["analyzed"] == 8 and out["failed"] == 0


def test_kv_write_failure_is_visible_on_tool_span(fake_kv, monkeypatch, traced_runs):
    """正文写失败：tool:kv.write 上直接看到失败的 key 与「已收手不写状态」。"""
    _seed(fake_kv, monkeypatch, count=1)
    failed_key = hw.AI_CACHE_PREFIX + "term 0"
    fake_kv.fail_keys.add(failed_key)

    with ls_trace("site-agent:analyze_hotwords"):
        out = hw.run_batch(limit=1, kv=fake_kv)

    span = next(r for r in traced_runs if r.name == "tool:kv.write")
    assert span.outputs["stoppedAfterTextFailure"] is True
    assert failed_key in span.outputs["writeErrors"]
    assert any(w["key"] == failed_key and w["ok"] is False
               for w in span.outputs["writes"])
    assert out["failed"] == 1 and out["items"][0]["writeErrors"] == [failed_key]
    assert fake_kv.get_json("saved", []) == [] and fake_kv.get_json("rejected", []) == []


def test_runtime_tools_emit_tool_spans_with_io(fake, traced_runs):
    """26 个 RuntimeTool 统一打点：成功带返回体，Input 校验失败要让 span 标红。"""
    ok = tool_registry.run("get_hotwords", limit=5)
    bad = tool_registry.run("get_flag")       # key 必填，故意不传

    good_span = next(r for r in traced_runs if r.name == "tool:get_hotwords")
    bad_span = next(r for r in traced_runs if r.name == "tool:get_flag")
    assert good_span.run_type == "tool" and bad_span.run_type == "tool"
    assert good_span.inputs == {"limit": 5}
    json.dumps(good_span.inputs, ensure_ascii=False)   # 上报前必须可序列化
    assert good_span.outputs["ok"] is True
    assert len(good_span.outputs["result"]["items"]) == 2
    assert good_span.extra["metadata"]["kind"] == "read"
    assert good_span.error in (None, "")

    assert ok["ok"] is True and bad["ok"] is False
    assert bad_span.outputs["ok"] is False
    assert bad_span.error and "key" in bad_span.error.lower()


def test_runtime_tool_span_nests_under_real_langgraph_node(fake, monkeypatch, traced_runs):
    """真实 graph 节点里调工具：tool:* span 挂在节点下，不是又一条孤立根。"""
    from typing import TypedDict

    from langchain_core.tracers.langchain import LangChainTracer
    from langgraph.graph import END, START, StateGraph

    monkeypatch.setattr(LangChainTracer, "_persist_run_single", lambda self, run: None)
    node_run_ids: list[str] = []

    class S(TypedDict):
        out: dict

    def node(state: S) -> dict:
        current = run_helpers.get_current_run_tree()
        node_run_ids.append(str(current.id) if current else "")
        return {"out": tool_registry.run("get_hotwords", limit=2)}

    graph = (StateGraph(S).add_node("tools", node)
             .add_edge(START, "tools").add_edge("tools", END).compile())
    result = graph.invoke({"out": {}}, config={"run_name": "site-agent:select_pages"})

    span = next(r for r in traced_runs if r.name == "tool:get_hotwords")
    assert node_run_ids and node_run_ids[0]
    assert str(span.parent_run_id) == node_run_ids[0]
    assert span.outputs["ok"] is True
    assert result["out"]["ok"] is True


def test_compact_io_and_trace_tool_zero_overhead(monkeypatch):
    """出入参清洗保留结构、截断长值；关闭追踪时 trace_tool 就是原函数调用。"""
    from src.app.observability import langsmith as ls
    from src.app.observability.langsmith import MAX_IO_CHARS, compact_io, trace_tool

    out = compact_io({"term": "x" * (MAX_IO_CHARS + 500),
                      "items": list(range(200)), "nested": {"ok": True}})
    assert out["term"].endswith("已截断]")
    assert len(out["items"]) == 61            # 60 项 + 1 条数量提示
    assert out["nested"]["ok"] is True

    monkeypatch.setattr(settings, "langsmith_tracing", False)
    monkeypatch.setattr(settings, "langsmith_api_key", "")

    def must_not_run(*args, **kwargs):
        raise AssertionError("关闭追踪时不该进入 run_traced")

    monkeypatch.setattr(ls, "run_traced", must_not_run)
    assert trace_tool(lambda: 42, name="tool:x", inputs={"a": 1}) == 42
