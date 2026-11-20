# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LangSmith 层级与工具可见性回归（声明式切面版）。

trace 全部由 observability.observed 切面声明，父子靠 contextvar 自动继承：
    外层 root → 热词 run → LangGraph → 节点 → 词 → IO / LLM
本文件验证三件事：
  ① 一条树：6 个词展开在同一条 trace 下，不散成独立根；LLM 挂在对应词下；
  ② 可观测：热词线与常规线共用 registry 的统一 tool span（tool:fetch_serp /
     tool:fetch_trend / tool:write_hotword_result / tool:next_candidate …），
     出入参是真实 ToolOutput；源挂了 span 标红且 retryable=True，业务照常降级，
     被跳过的取数不伪造 span，只在词 span 上留 note；
  ③ 零开销：关追踪时不建 span、不注入回链字段、不 import SDK 运行时。

父子断言用「后代关系」而不是「直接 parent」——LangGraph 会自动插入图/节点层，
切面不该假设中间没有这些层；重要的是整棵树连通、叶子挂在正确的词下。
"""

from __future__ import annotations

import json

import pytest
from langsmith import client as ls_client, run_helpers, schemas as ls_schemas
from langsmith import trace as ls_trace, traceable, utils
from langsmith.run_trees import RunTree

from src.app.config import settings
from src.app.enums import TraceName
from src.app.tools.adapters.cloudflare import kv as kvk
from src.app.tools.adapters.hotwords import hotword_contract as hwk
from src.app.tools.adapters.hotwords import hotword_fetch
from src.app.tools.adapters.hotwords import hotword_llm
from src.app.tools.adapters.hotwords import scrape_serp, scrape_trend
from src.app.tools.adapters.hotwords.source_errors import SourceUnavailable
from src.app.graph import hotword_graph
from src.app.tools.registry import tool_registry


@pytest.fixture
def traced_runs(monkeypatch):
    """打开追踪但不出网：post 收集 RunTree，patch 空转。"""
    monkeypatch.setattr(settings, "langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "lsv2_pt_fake")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_fake")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    utils.get_env_var.cache_clear()
    monkeypatch.setattr(utils, "tracing_is_enabled", lambda ctx=None: True)
    monkeypatch.setattr(ls_client.Client, "info",
                        property(lambda self: ls_schemas.LangSmithInfo()))
    runs: list[RunTree] = []
    monkeypatch.setattr(RunTree, "post", lambda self: runs.append(self))
    monkeypatch.setattr(RunTree, "patch", lambda self: None)
    yield runs
    utils.get_env_var.cache_clear()


def _seed(fake_kv, monkeypatch, count=6, llm=None):
    """离线铺候选词：SERP / Trends / LLM 全部替身，不碰任何外部服务。"""
    monkeypatch.setattr(hotword_fetch, "SERP_GAP_SEC", 0)
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(count)])
    monkeypatch.setattr(scrape_serp, "fetch_serp",
                        lambda term, session=None, limit=10: {
                            "engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(scrape_trend, "fetch_interest",
                        lambda term, session=None, geo="": {
                            "points": [{"d": "2026-01-01", "v": 50}],
                            "summary": {"avg": 50}, "attempts": []})
    monkeypatch.setattr(hotword_llm, "_llm", llm or (lambda messages: (
        "7) 结论：观望，等长尾\n8) 信号标签：工具查询", "fake")))


@traceable(run_type="llm", name="ChatOpenAI")
def _fake_openai_call(term: str) -> dict:
    """模拟 wrap_openai 装饰过的 create：验证它把父 run 认成词 span。"""
    return {"choices": [{"message": {"content": "ok"}}]}


def _under(runs: list[RunTree], run: RunTree, ancestor_id: str) -> bool:
    """run 是否是 ancestor_id 的后代（含直接子节点）；切面不假设中间层数。"""
    by_id = {str(r.id): r for r in runs}
    cur = run
    seen: set[str] = set()
    while cur is not None and str(cur.id) not in seen:
        pid = str(cur.parent_run_id) if cur.parent_run_id else ""
        if pid == ancestor_id:
            return True
        seen.add(str(cur.id))
        cur = by_id.get(pid)
    return False


def test_batch_tree_is_nested_under_graph_root(fake_kv, monkeypatch, traced_runs):
    """一条根展开看完 6 个词：run 挂外层根、每词在 run 下、LLM 在对应词下。"""
    seen_in_llm: list[tuple[str, str]] = []

    def llm(messages):
        current = run_helpers.get_current_run_tree()
        seen_in_llm.append((current.name, str(current.id)) if current else ("", ""))
        term = messages[-1]["content"].split("关键词：")[1].split("\n")[0]
        _fake_openai_call(term)
        return "7) 结论：观望，等长尾\n8) 信号标签：工具查询", "fake"

    _seed(fake_kv, monkeypatch, count=6, llm=llm)

    with ls_trace("site-agent:analyze_hotwords") as root:
        out = hotword_graph.run_hotwords(limit=6)

    run = next(r for r in traced_runs if r.name == TraceName.HOTWORD_RUN)
    words = [r for r in traced_runs if r.name.startswith(hwk.WORD_TRACE_PREFIX)]
    llms = [r for r in traced_runs if r.name == "ChatOpenAI"]

    assert str(run.parent_run_id) == str(root.id)   # 根切面挂外层 root
    assert len(words) == 6 and len(llms) == 6
    assert all(_under(traced_runs, w, str(run.id)) for w in words)
    # 每个 LLM run 在某词 span 下（contextvar 自动继承），不是散落的根
    assert all(any(_under(traced_runs, r, str(w.id)) for w in words) for r in llms)
    assert len(seen_in_llm) == 6
    assert all(name.startswith(hwk.WORD_TRACE_PREFIX) for name, _ in seen_in_llm)
    # 每个词 span 都有自己的 run，没有互相串
    assert len({wid for _, wid in seen_in_llm}) == 6

    assert out["analyzed"] == 6
    assert out["traceRun"]["runId"].startswith("hotwords-")
    assert out["traceRun"]["traceRunId"] == str(run.id)
    assert out["traceRun"]["traceId"] == str(root.id)
    # 主线程 contextvar 在切面退出后干净归位
    assert run_helpers.get_current_run_tree() is None


def test_word_span_marks_business_error_without_breaking_others(fake_kv, monkeypatch,
                                                                traced_runs):
    """某词 LLM 抛异常：该词 span 标红写 error，其余词照常完成且在同一棵树下。"""
    def llm(messages):
        term = messages[-1]["content"].split("关键词：")[1].split("\n")[0]
        if term.endswith("2"):
            raise RuntimeError("LLM 连接超时")
        return "7) 结论：观望，等长尾\n8) 信号标签：工具查询", "fake"

    _seed(fake_kv, monkeypatch, count=5, llm=llm)

    with ls_trace("site-agent:analyze_hotwords") as root:
        out = hotword_graph.run_hotwords(limit=5)

    run = next(r for r in traced_runs if r.name == TraceName.HOTWORD_RUN)
    words = [r for r in traced_runs if r.name.startswith(hwk.WORD_TRACE_PREFIX)]
    assert str(run.parent_run_id) == str(root.id)
    assert len(words) == 5
    assert all(_under(traced_runs, w, str(run.id)) for w in words)
    failed = [w for w in words if w.error]
    assert len(failed) == 1 and "LLM 连接超时" in failed[0].error
    assert out["analyzed"] == 4 and out["failed"] == 1
    assert run.outputs["analyzed"] == 4


def test_batch_nests_under_real_langgraph_node(fake_kv, monkeypatch, traced_runs):
    """真实 LangGraph 节点里调用（不是手工造的根）：run span 要挂在节点 run 下。"""
    from typing import TypedDict

    from langchain_core.runnables.config import var_child_runnable_config
    from langchain_core.tracers.langchain import LangChainTracer
    from langgraph.graph import END, START, StateGraph

    monkeypatch.setattr(LangChainTracer, "_persist_run_single", lambda self, run: None)
    _seed(fake_kv, monkeypatch, count=3)

    node_run_ids: list[str] = []

    class S(TypedDict):
        out: dict

    def node(state: S) -> dict:
        current = run_helpers.get_current_run_tree()
        node_run_ids.append(str(current.id) if current else "")
        # langgraph 0.3.x 对「被当作节点调用的图」强制 values 模式（Pregel._defaults），
        # 而两条线都是独立根图、共用驱动器按 updates 取增量。这里只清掉继承来的 pregel
        # 任务配置，LangSmith 的 run tree 上下文保留——本用例验的就是 span 父子关系。
        token = var_child_runnable_config.set(None)
        try:
            return {"out": hotword_graph.run_hotwords(limit=3)}
        finally:
            var_child_runnable_config.reset(token)

    graph = (StateGraph(S).add_node("analyze", node)
             .add_edge(START, "analyze").add_edge("analyze", END).compile())
    result = graph.invoke({"out": {}},
                          config={"run_name": "site-agent:analyze_hotwords"})

    run = next(r for r in traced_runs if r.name == TraceName.HOTWORD_RUN)
    words = [r for r in traced_runs if r.name.startswith(hwk.WORD_TRACE_PREFIX)]
    assert node_run_ids and node_run_ids[0]
    assert str(run.parent_run_id) == node_run_ids[0]
    assert len(words) == 3
    # 中间节点 run 被本用例 stub 掉持久化，traced_runs 里没有它们，
    # 无法沿 parent 链回溯；只断言词 span 都在 run 之下即可（完整层级由首个用例验）。
    assert all(str(w.parent_run_id) for w in words)
    assert result["out"]["analyzed"] == 3


def test_early_return_still_has_batch_span_and_link(monkeypatch, traced_runs):
    """KV 读挂了提前返回：run span 照样记录 error，返回照样带根 run 回链。"""
    class BoomKV:
        def get_json(self, *a, **k):
            raise RuntimeError("unreachable")

    monkeypatch.setattr(hwk, "get_kv", lambda: BoomKV())
    with ls_trace("site-agent:analyze_hotwords") as root:
        out = hotword_graph.run_hotwords(limit=5)

    run = next(r for r in traced_runs if r.name == TraceName.HOTWORD_RUN)
    assert str(run.parent_run_id) == str(root.id)
    assert "候选查询失败" in out["error"]
    assert out["traceRun"]["traceRunId"] == str(run.id)
    assert run.outputs["error"].startswith("候选查询失败")


def test_tracing_off_has_no_spans_and_plain_result(fake_kv, monkeypatch):
    """关闭追踪时零开销：正常出结果，不生成 traceRun，也不 import langsmith 运行时。"""
    monkeypatch.setattr(settings, "langsmith_tracing", False)
    monkeypatch.setattr(settings, "langsmith_api_key", "")
    _seed(fake_kv, monkeypatch, count=3)

    out = hotword_graph.run_hotwords(limit=3)
    assert out["analyzed"] == 3
    assert "traceRun" not in out
    assert run_helpers.get_current_run_tree() is None


# ---------------------------------------------------------------------------
# 工具可见性：热词线与常规线共用 registry 的 30 个 RuntimeTool，都要看得到出入参
# ---------------------------------------------------------------------------
def test_run_tool_spans_capture_serp_trend_kv_io(fake_kv, monkeypatch, traced_runs):
    """每词三条 tool span + 每轮一次取词 + 判例读一次，父链与出入参都要对。"""
    def llm(messages):
        _fake_openai_call("x")
        return "7) 结论：不做，影视作品\n8) 信号标签：工具查询", "fake"

    _seed(fake_kv, monkeypatch, count=3, llm=llm)
    with ls_trace("site-agent:analyze_hotwords") as root:
        out = hotword_graph.run_hotwords(limit=3)

    run = next(r for r in traced_runs if r.name == TraceName.HOTWORD_RUN)
    words = [r for r in traced_runs if r.name.startswith(hwk.WORD_TRACE_PREFIX)]
    tools = [r for r in traced_runs if r.run_type == "tool"]
    assert str(run.parent_run_id) == str(root.id)
    assert len(words) == 3
    # 3 词 × (fetch_serp/fetch_trend/write_hotword_result) + 3 次取词（达 limit 即停）+ 1 条判例读
    assert len(tools) == 13

    readers = [r for r in tools if r.name == "tool:next_candidate"]
    assert len(readers) == 3 and all(_under(traced_runs, r, str(run.id)) for r in readers)
    assert readers[0].inputs["seen"] == []
    assert readers[0].outputs["ok"] is True
    assert readers[0].outputs["result"]["term"] == "term 0"
    assert readers[-1].outputs["result"]["term"] == "term 2"

    ex_readers = [r for r in tools if r.name == "tool:read_examples"]
    assert len(ex_readers) == 1 and _under(traced_runs, ex_readers[0], str(run.id))
    assert ex_readers[0].outputs["ok"] is True

    per_word = [r for r in tools if r not in readers and r not in ex_readers]
    word_ids = {str(w.id) for w in words}
    assert all(any(_under(traced_runs, r, wid) for wid in word_ids) for r in per_word)
    for name in ("tool:fetch_serp", "tool:fetch_trend", "tool:write_hotword_result"):
        assert sum(1 for r in per_word if r.name == name) == 3

    serp = next(r for r in per_word if r.name == "tool:fetch_serp")
    assert serp.inputs == {"term": "term 0"}
    assert serp.outputs["ok"] is True
    body = serp.outputs["result"]
    assert body["fromCache"] is False
    assert "itemCount" in body and "competition" in body
    serp_keys = {w["key"] for w in body["writes"]}
    assert any(k.startswith(kvk.SERP_PREFIX) for k in serp_keys)
    assert kvk.COMPETITION_KEY in serp_keys
    assert all(w["ok"] for w in body["writes"])
    # 适配器 note 的诊断属于这次工具调用，不漂到外层词 span
    assert serp.outputs["engine"] == "duckduckgo"

    trend = next(r for r in per_word if r.name == "tool:fetch_trend")
    assert trend.inputs == {"term": "term 0"}
    tbody = trend.outputs["result"]
    assert tbody["ok"] is True and "pointCount" in tbody and tbody["writes"]
    assert trend.outputs["pointCount"] == 1

    kvwrite = next(r for r in per_word if r.name == "tool:write_hotword_result")
    assert kvwrite.inputs["verdict"] == "不做"
    assert kvwrite.inputs["item"]["term"] == "term 0"
    wbody = kvwrite.outputs["result"]
    assert wbody["textChars"] > 0 and wbody["textPreview"]
    assert wbody["status"] == "rejected"
    assert kvk.AI_CACHE_PREFIX + kvk.term_key("term 0") in {
        w["key"] for w in wbody["writes"]}
    assert wbody["writeErrors"] == []

    llms = [r for r in traced_runs if r.name == "ChatOpenAI"]
    assert len(llms) == 3
    assert all(any(_under(traced_runs, r, wid) for wid in word_ids) for r in llms)
    assert out["analyzed"] == 3


def test_source_down_marks_tool_spans_red_and_batch_continues(fake_kv, monkeypatch,
                                                             traced_runs):
    """源整体挂掉：只撞一次，span 标红带真因且 retryable=True；后续词跳过取数
    （没有工具调用就不伪造 span，只在词 span 上留 note），整轮不中止。"""
    _seed(fake_kv, monkeypatch, count=8)

    def serp_boom(term, session=None, limit=10):
        raise SourceUnavailable("duckduckgo", "连不上 DuckDuckGo 出口", [])

    def trend_boom(term, session=None, geo=""):
        raise SourceUnavailable("google-trends", "Google Trends 连接超时", [])

    monkeypatch.setattr(scrape_serp, "fetch_serp", serp_boom)
    monkeypatch.setattr(scrape_trend, "fetch_interest", trend_boom)

    with ls_trace("site-agent:analyze_hotwords"):
        out = hotword_graph.run_hotwords(limit=8)

    serp_spans = [r for r in traced_runs if r.name == "tool:fetch_serp"]
    trend_spans = [r for r in traced_runs if r.name == "tool:fetch_trend"]
    assert len(serp_spans) == 1 and len(trend_spans) == 1
    assert "连不上 DuckDuckGo 出口" in serp_spans[0].error
    assert "Google Trends 连接超时" in trend_spans[0].error
    assert serp_spans[0].outputs["ok"] is False
    assert serp_spans[0].outputs["retryable"] is True
    assert trend_spans[0].outputs["retryable"] is True

    words = [r for r in traced_runs if r.name.startswith(hwk.WORD_TRACE_PREFIX)]
    assert len(words) == 8
    skipped = [w for w in words if w.outputs.get("serpSkipped")]
    assert len(skipped) == 7                       # 首词真撞了一次，其余 7 词跳过
    assert all(w.outputs.get("trendSkipped") for w in skipped)
    assert any("SERP 未取到" in (w.outputs.get("notice") or "") for w in words)

    assert out["serpDisabled"] is True and out["trendsDisabled"] is True
    assert "SERP 源不可用" in out["note"] and "趋势源不可用" in out["note"]
    assert out["analyzed"] == 8 and out["failed"] == 0


def test_kv_write_failure_is_visible_on_tool_span(fake_kv, monkeypatch, traced_runs):
    """正文写失败：tool:write_hotword_result 上直接看到失败的 key 与「已收手不写状态」。"""
    _seed(fake_kv, monkeypatch, count=1)
    failed_key = kvk.AI_CACHE_PREFIX + "term 0"
    fake_kv.fail_keys.add(failed_key)

    with ls_trace("site-agent:analyze_hotwords"):
        out = hotword_graph.run_hotwords(limit=1)

    span = next(r for r in traced_runs if r.name == "tool:write_hotword_result")
    body = span.outputs["result"]
    assert body["stoppedAfterTextFailure"] is True
    assert failed_key in body["writeErrors"]
    assert any(w["key"] == failed_key and w["ok"] is False for w in body["writes"])
    assert out["failed"] == 1 and out["items"][0]["writeErrors"] == [failed_key]
    assert fake_kv.get_json("saved", []) == [] and fake_kv.get_json("rejected", []) == []


def test_runtime_tools_emit_tool_spans_with_io(fake, traced_runs):
    """30 个 RuntimeTool 统一打点：成功带返回体，Input 校验失败要让 span 标红。"""
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
        raise AssertionError("关闭追踪时不该做出入参清洗")

    # _compact_envelope 只在追踪开启分支里跑；关掉后 trace_tool 必须直接调用 func
    monkeypatch.setattr(ls, "_compact_envelope", must_not_run)
    assert trace_tool(lambda: 42, name="tool:x", inputs={"a": 1}) == 42
