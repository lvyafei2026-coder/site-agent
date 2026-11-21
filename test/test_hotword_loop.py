# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""逐词循环图（nodes/hotword_*_node.py + graph/edges.py + graph/hotword_graph.py）的图机制单测：
条件边回环、limit 封顶、名单 → 连线、串行推进。

不重复 test_hotwords.py 的写回/降级语义（那些已被 run_hotwords 端到端覆盖），这里只钉
「循环由图驱动、每轮问 tool 取一个词」这件事本身：外层节点拓扑、子图按名单连线、
三个路由函数的分支、fetch_one 真的每轮取一个、limit 真能提前收口。
"""

from __future__ import annotations

from langgraph.graph import END

from src.app.enums import GraphNode, RouteTag
from src.app.graph import get_hotword_graph, hotword_graph
from src.app.graph.edges import (
    FETCH_EDGE_MAP, WORD_EDGE_MAP, route_after_fetch, route_after_llm, route_after_word,
)
from src.app.graph.hotword_graph import DEFAULT_EVIDENCE, DEFAULT_POST, build_word_graph
from src.app.graph.state import initial_hotword_state
from src.app.nodes.hotword_fetch_node import fetch_one
from src.app.tools.adapters.cloudflare import kv as kvk
from src.app.tools.adapters.hotwords import hotword_contract
from src.app.tools.adapters.hotwords import hotword_llm
from src.app.tools.adapters.hotwords import scrape_serp, scrape_trend


def _nodes(word_graph):
    return set(word_graph.get_graph().nodes.keys())


def test_loop_graph_topology():
    """外层三个业务节点（取词 / 逐词子图 / 汇总），回环由 analyze_word 的条件边给出。"""
    nodes = _nodes(get_hotword_graph())
    assert {"fetch_one", "analyze_word", "build_summary"} <= nodes
    assert END in nodes
    # 步骤节点在外层不可见——它们是子图内部的事（trace 因此保持逐词粒度）
    assert not {"gather_serp", "llm_analyze", "collect_word"} & nodes


def test_word_graph_is_wired_from_step_names():
    """★名单就是连线：不在名单上的步骤，图上根本没有那个节点。"""
    assert _nodes(build_word_graph(DEFAULT_EVIDENCE, DEFAULT_POST)) >= {
        "gather_serp", "gather_trend", "llm_analyze", "write_result", "collect_word"}

    serp_only = _nodes(build_word_graph(("serp",), ()))
    assert {"gather_serp", "llm_analyze", "collect_word"} <= serp_only
    assert not {"gather_trend", "write_result"} & serp_only

    no_step = _nodes(build_word_graph((), ()))          # 两段名单都空
    assert no_step >= {"llm_analyze", "collect_word"}
    assert not {"gather_serp", "gather_trend", "write_result"} & no_step


def test_route_after_fetch_and_word():
    """两道闸门：取到词才进子图；跑完一个词没达上限就回 fetch_one——与常规线 edges 同一套写法。"""
    base = {"limit": 5, "has_more": True, "loop_count": 0, "load_error": ""}
    assert route_after_fetch(base) == RouteTag.ANALYZE                              # 有词 → 子图
    assert route_after_fetch({**base, "has_more": False}) == RouteTag.SUMMARIZE     # tool 取尽
    assert route_after_fetch({**base, "load_error": "boom"}) == RouteTag.SUMMARIZE  # KV 失败
    assert FETCH_EDGE_MAP == {RouteTag.ANALYZE: GraphNode.ANALYZE_WORD,
                              RouteTag.SUMMARIZE: GraphNode.SUMMARY}

    assert route_after_word(base) == RouteTag.LOOP                     # 还有额度 → 回环
    assert route_after_word({**base, "loop_count": 5}) == RouteTag.SUMMARIZE   # 达上限
    assert WORD_EDGE_MAP == {RouteTag.LOOP: GraphNode.FETCH_ONE,
                             RouteTag.SUMMARIZE: GraphNode.SUMMARY}


def test_route_after_llm_skips_post_actions_without_conclusion():
    """LLM 没产出（失败 / 空正文）就不做后置动作：不给没结论的词写 KV。"""
    assert route_after_llm({"ai": {"text": "7) 结论：做"}}) == RouteTag.NEXT
    assert route_after_llm({"ai": {}}) == RouteTag.COLLECT


def test_fetch_one_asks_tool_each_round(fake_kv, monkeypatch):
    """每轮 fetch 都真问一次 tool 取一个词；seen 排除本轮已取的，避免重复。"""
    fake_kv.put_json("candidates", [{"term": f"t{i}"} for i in range(4)])
    monkeypatch.setattr(scrape_serp, "fetch_serp", lambda *a, **k: {"engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(scrape_trend, "fetch_interest", lambda *a, **k: {"points": [], "summary": {}, "attempts": []})
    monkeypatch.setattr(hotword_llm, "_llm", lambda m: ("7) 结论：观望\n8) 信号标签：无", "fake"))

    s = initial_hotword_state(limit=10)
    s1 = {**s, **fetch_one(s)}
    assert s1["has_more"] is True and s1["current"]["term"] == "t0"
    assert s1["seen"] == {kvk.term_key("t0")}
    # 第二轮：再问一次 tool，取到下一个词（不是从内存游标推进）
    s2 = {**s1, **fetch_one(s1)}
    assert s2["has_more"] is True and s2["current"]["term"] == "t1"
    assert s2["seen"] == {kvk.term_key("t0"), kvk.term_key("t1")}
    # 取尽后 tool 返回 None → has_more=False，条件边收尾
    s3 = {**s2, **fetch_one(s2)}
    s4 = {**s3, **fetch_one(s3)}
    assert s4["has_more"] is True and s4["current"]["term"] == "t3"
    s5 = {**s4, **fetch_one(s4)}
    assert s5["has_more"] is False and s5["current"] is None


def test_loop_stops_at_limit(fake_kv, monkeypatch):
    """给 8 个候选、limit=3，端到端只分析 3 个词——limit 是图路由的真实封顶。"""
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(8)])
    monkeypatch.setattr(scrape_serp, "fetch_serp", lambda *a, **k: {"engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(scrape_trend, "fetch_interest", lambda *a, **k: {"points": [], "summary": {}, "attempts": []})
    monkeypatch.setattr(hotword_llm, "_llm", lambda m: ("7) 结论：观望\n8) 信号标签：无", "fake"))

    out = hotword_graph.run_hotwords(limit=3)
    assert out["fetched"] == 3 and len(out["items"]) == 3
    # 未分析的词不会写 AI 缓存（只回写了 3 个）
    analyzed_keys = {kvk.term_key(f"term {i}") for i in range(3)}
    left_keys = {kvk.term_key(f"term {i}") for i in range(3, 8)}
    assert all(fake_kv.get_text(kvk.AI_CACHE_PREFIX + k) for k in analyzed_keys)
    assert not any(fake_kv.get_text(kvk.AI_CACHE_PREFIX + k) for k in left_keys)


def test_loop_kv_unreachable_short_circuits(monkeypatch):
    """候选查询抛异常时，图经条件边走 build_summary 收口为 ok=False。"""
    class BoomKV:
        def get_json(self, *a, **k):
            raise RuntimeError("kv down")

        def list_keys(self, *a, **k):
            raise RuntimeError("kv down")

    monkeypatch.setattr(hotword_contract, "get_kv", lambda: BoomKV())
    out = hotword_graph.run_hotwords(limit=5)
    assert out["ok"] is False and "候选查询失败" in out["error"]
    assert out["items"] == [] and out["fetched"] == 0
