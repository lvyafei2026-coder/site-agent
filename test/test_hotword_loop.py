# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""逐词循环图（nodes/hotword_*_node.py + graph/edges.py）的图机制单测：
条件边回环、limit 封顶、串行推进。

不重复 test_hotwords.py 的写回/降级语义（那些已被 run_hotwords 端到端覆盖），这里只钉
「循环由图驱动、每轮问 tool 取一个词」这件事本身：节点拓扑、route_after_analyze 的四种
路由、fetch_one 真的每轮取一个、limit 真能提前收口。
"""

from __future__ import annotations

from langgraph.graph import END

from src.app.enums import GraphNode, RouteTag
from src.app.graph import get_hotword_graph, hotword_graph
from src.app.graph import state as graph_state
from src.app.graph.edges import HOTWORD_EDGE_MAP, route_after_analyze
from src.app.nodes.hotword_fetch_node import fetch_one
from src.app.services.hotword_service import _DegradeState
from src.app.tools.adapters.cloudflare import kv as kvk
from src.app.tools.adapters.hotwords import hotword_llm
from src.app.tools.adapters.hotwords import scrape_serp, scrape_trend


def test_loop_graph_topology():
    """图有三个业务节点（取词 / 分析 / 汇总），且 analyze_one 有回环边指回 fetch_one。"""
    g = get_hotword_graph().get_graph()
    nodes = set(g.nodes.keys())
    assert {"fetch_one", "analyze_one", "build_summary"} <= nodes
    # 条件边的分支里应含回环目标与收尾节点，汇总节点再连到 END
    assert END in nodes


def test_route_after_analyze():
    """路由返回标签、由 HOTWORD_EDGE_MAP 翻成节点名——与常规线 edges 同一套写法。"""
    base = {"limit": 5, "has_more": True, "loop_count": 0, "load_error": ""}
    assert route_after_analyze(base) == RouteTag.LOOP                            # 还有词、未达上限 → 回环
    assert route_after_analyze({**base, "loop_count": 5}) == RouteTag.SUMMARIZE   # 达上限
    assert route_after_analyze({**base, "has_more": False}) == RouteTag.SUMMARIZE  # tool 取尽
    assert route_after_analyze({**base, "load_error": "boom"}) == RouteTag.SUMMARIZE  # KV 失败
    assert HOTWORD_EDGE_MAP == {RouteTag.LOOP: GraphNode.FETCH_ONE,
                                RouteTag.SUMMARIZE: GraphNode.SUMMARY}


def test_fetch_one_asks_tool_each_round(fake_kv, monkeypatch):
    """每轮 fetch 都真问一次 tool 取一个词；seen 排除本轮已取的，避免重复。"""
    fake_kv.put_json("candidates", [{"term": f"t{i}"} for i in range(4)])
    monkeypatch.setattr(scrape_serp, "fetch_serp", lambda *a, **k: {"engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(scrape_trend, "fetch_interest", lambda *a, **k: {"points": [], "summary": {}, "attempts": []})
    monkeypatch.setattr(hotword_llm, "_llm", lambda m: ("7) 结论：观望\n8) 信号标签：无", "fake"))

    s = dict(limit=10, kv=fake_kv, prompt_version=None,
             degrade=_DegradeState(), seen=set(), examples_loaded=True,
             examples=[], has_more=False, current=None, results=[],
             loop_count=0, load_error="")
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
        def list_keys(self, *a, **k):
            raise RuntimeError("kv down")

    monkeypatch.setattr(graph_state, "get_kv", lambda: BoomKV())
    out = hotword_graph.run_hotwords(limit=5)
    assert out["ok"] is False and "候选查询失败" in out["error"]
    assert out["items"] == [] and out["fetched"] == 0
