# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词批次的 LangSmith 层级：graph 根 → 批次 → 每词 → ChatOpenAI 必须是一棵树。

回归背景（2026-11-13）：ThreadPoolExecutor 的 worker 线程不继承主线程的 contextvars，
LangSmith 的父 run 存在 contextvar 里，于是 20 个 wrap_openai 调用全变成孤立根 trace
（Traces 页面「连不起来」）。这里用真实 SDK（只把 post/patch 换成内存收集）验证
显式 parent 能钉死父子链，且 LLM run 真的挂在对应词 span 下。
"""

from __future__ import annotations

import pytest
from langsmith import client as ls_client, run_helpers, schemas as ls_schemas
from langsmith import trace as ls_trace, traceable, utils
from langsmith.run_trees import RunTree

from src.app.config import settings
from src.app.tools.adapters.sources import hotwords as hw


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
