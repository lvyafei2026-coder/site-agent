# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""逐词循环图：图拓扑、D1 取词、循环上限与失败收口。"""

from __future__ import annotations

from langgraph.graph import END

from src.app.enums import GraphNode, RouteTag
from src.app.graph import get_hotword_graph, hotword_graph
from src.app.graph.edges import (
    FETCH_EDGE_MAP, WORD_EDGE_MAP, route_after_fetch, route_after_llm, route_after_word,
)
from src.app.graph.hotword_graph import DEFAULT_EVIDENCE, DEFAULT_POST, build_word_graph
from src.app.graph.state import initial_hotword_state
from src.app.nodes.hotword_fetch_node import fetch_one, prepare_term
from src.app.tools.adapters.hotwords import wordlib, scrape_serp, scrape_trend
from src.app.utils import llm_io


def _nodes(word_graph):
    return set(word_graph.get_graph().nodes.keys())


def _seed(fake_d1, count=4):
    for i in range(count):
        term_id = fake_d1.seed_term(f"t{i}")
        fake_d1.seed_candidate(term_id, score=100 - i)


def test_loop_graph_topology():
    nodes = _nodes(get_hotword_graph())
    assert {"fetch_one", "analyze_word", "build_summary"} <= nodes
    assert END in nodes
    assert not {"gather_serp", "llm_analyze", "collect_word"} & nodes


def test_term_graph_topology():
    nodes = _nodes(hotword_graph.get_term_graph())
    assert {"prepare_term", "analyze_word", "build_summary"} <= nodes
    assert END in nodes
    assert "fetch_one" not in nodes


def test_prepare_term_takes_named_term(fake_d1):
    term = "november full moon"
    term_id = fake_d1.seed_term(term)
    fake_d1.seed_status(term_id, status="wip")
    fake_d1.seed_analysis(term_id, text="旧分析正文")
    state = initial_hotword_state(1, term=term)
    out = prepare_term(state)
    assert out["has_more"] is True
    assert out["current"]["term"] == term
    assert out["word"]["term"] == term
    assert out["body"]["term"] == term
    assert out["examples_loaded"] is True


def test_prepare_term_missing_term_closes(fake_d1):
    out = prepare_term(initial_hotword_state(1, term="no such term"))
    assert out["has_more"] is False and "找不到" in out["load_error"]


def test_word_graph_is_wired_from_step_names():
    assert _nodes(build_word_graph(DEFAULT_EVIDENCE, DEFAULT_POST)) >= {
        "gather_serp", "gather_trend", "llm_analyze", "write_result", "collect_word"}
    serp_only = _nodes(build_word_graph(("serp",), ()))
    assert {"gather_serp", "llm_analyze", "collect_word"} <= serp_only
    assert not {"gather_trend", "write_result"} & serp_only
    no_step = _nodes(build_word_graph((), ()))
    assert no_step >= {"llm_analyze", "collect_word"}
    assert not {"gather_serp", "gather_trend", "write_result"} & no_step


def test_route_after_fetch_and_word():
    base = {"limit": 5, "has_more": True, "loop_count": 0, "load_error": ""}
    assert route_after_fetch(base) == RouteTag.ANALYZE
    assert route_after_fetch({**base, "has_more": False}) == RouteTag.SUMMARIZE
    assert route_after_fetch({**base, "load_error": "D1 unavailable"}) == RouteTag.SUMMARIZE
    assert FETCH_EDGE_MAP == {RouteTag.ANALYZE: GraphNode.ANALYZE_WORD,
                              RouteTag.SUMMARIZE: GraphNode.SUMMARY}
    assert route_after_word(base) == RouteTag.LOOP
    assert route_after_word({**base, "loop_count": 5}) == RouteTag.SUMMARIZE
    assert WORD_EDGE_MAP == {RouteTag.LOOP: GraphNode.FETCH_ONE,
                             RouteTag.SUMMARIZE: GraphNode.SUMMARY}


def test_route_after_llm_skips_post_actions_without_conclusion():
    assert route_after_llm({"ai": {"text": "7) 结论：做"}}) == RouteTag.NEXT
    assert route_after_llm({"ai": {}}) == RouteTag.COLLECT


def test_fetch_one_reads_one_candidate_per_round(fake_d1):
    _seed(fake_d1, 4)
    state = initial_hotword_state(limit=10)
    first = {**state, **fetch_one(state)}
    assert first["has_more"] is True and first["current"]["term"] == "t0"
    assert first["seen"] == {"t0"}
    second = {**first, **fetch_one(first)}
    assert second["has_more"] is True and second["current"]["term"] == "t1"
    assert second["seen"] == {"t0", "t1"}


def test_loop_stops_at_limit(fake_d1, monkeypatch):
    _seed(fake_d1, 8)
    monkeypatch.setattr(scrape_serp, "fetch_serp",
                        lambda *a, **k: {"engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(scrape_trend, "fetch_interest",
                        lambda *a, **k: {"points": [], "summary": {}, "attempts": []})
    monkeypatch.setattr(llm_io, "complete",
                        lambda m, **_: ("7) 结论：观望\n8) 信号标签：无", "fake"))
    out = hotword_graph.run_hotwords(limit=3)
    assert out["fetched"] == 3 and len(out["items"]) == 3
    rows = fake_d1.query("SELECT COUNT(*) AS count FROM radar_term_analyses")
    assert rows[0]["count"] == 3


def test_loop_stops_when_no_candidates(fake_d1):
    out = hotword_graph.run_hotwords(limit=5)
    assert out["ok"] is True and out["fetched"] == 0 and out["items"] == []


def test_loop_d1_unavailable_short_circuits(fake_d1, monkeypatch):
    monkeypatch.setattr(wordlib.database, "query",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("D1 unavailable")))
    out = hotword_graph.run_hotwords(limit=5)
    assert out["ok"] is False and "D1" in out["error"]
    assert out["items"] == [] and out["fetched"] == 0
