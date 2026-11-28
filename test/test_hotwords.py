# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""候选池分析：提示词拼装、结论/红线/标签判据、作品硬证据、意图→工具映射，以及
run_hotwords 的分析判据、提示词拼装与 D1 读写。

候选、人工状态、分析正文和证据统一由 fake_d1 提供；外部抓取与 LLM 使用 monkeypatch。
"""

import inspect

import pytest

from src.app.enums import CurationStatus, HintName, HotwordStep, Intent, RejectSource, ToolKind, Verdict
from src.app.prompts import hotword_messages as hm
from src.app.tools.adapters.hotwords import fetch, writeback
from src.app.tools.adapters.hotwords import scrape_serp, scrape_trend
from src.app.tools.adapters.hotwords import wordlib
from src.app.utils import llm_io
from src.app.graph import hotword_graph
from src.app.graph.hotword_graph import resolve_steps
from src.app.nodes.hotword_llm_node import decide, redline_of, signals_of, verdict_of
from src.app.nodes.hotword_serp_node import serp_brief, work_signal
from src.app.services.tool_service import INTENT_TOOLS, STEP_TOOLS
from src.app.tools.registry import tool_registry


def test_build_messages_keeps_serp_titles():
    msgs = hm.build_messages({
        "term": "nayyi naveli",
        "related": ["a", "b", "c", "d", "e", "f", "g"],
        "hints": ["工具查询"],
        "geoCount": 3, "traffic": 900,
        "serp": [{"domain": "imdb.com", "title": "Nayyi Naveli (2026 film)"},
                 {"domain": "en.wikipedia.org", "title": "Nayyi Naveli"}],
    })
    assert len(msgs) == 2 and msgs[0]["role"] == "system"
    assert "8) 信号标签" in msgs[0]["content"]
    user = msgs[1]["content"]
    assert "Nayyi Naveli (2026 film)" in user    # 辨形全靠标题
    assert "关联查询：a / b / c / d / e / f\n" in user   # 关联词最多 6 条
    assert " / g" not in user
    assert "命中的常青信号：工具查询" in user
    assert "覆盖地区数：3 个" in user


def test_build_messages_without_serp():
    user = hm.build_messages({"term": "x", "related": [], "hints": []})[1]["content"]
    assert "关联查询：（无）" in user
    assert "现有搜索结果" not in user
    assert "未命中明显常青信号" in user


def test_pick_examples_only_manual_with_generic_reason():
    """判例只取人工标记 + 有泛化价值理由；排除 AI 判定、占位理由、纯释义。"""
    rows = [
        {"term": "how to sound better", "reason": "改写润色，要的是一次性答案", "source": "manual", "updatedAt": 500},
        {"term": "doomsday", "reason": "世界末日", "source": "manual", "updatedAt": 490},       # 纯释义<4字? 4字，但无模式——靠去重/长度
        {"term": "some ai term", "reason": "AI 判定不做", "source": "ai", "updatedAt": 480},   # AI 判定排除
        {"term": "foo", "reason": "搜索意图不是找工具，做站没流量", "source": "manual", "updatedAt": 470},  # 已固化，跳过
        {"term": "bar", "reason": "热度下降", "source": "manual", "updatedAt": 460},            # 已固化，跳过
        {"term": "", "reason": "有理由但没词", "source": "manual", "updatedAt": 455},
        {"term": "baz", "reason": "", "source": "manual", "updatedAt": 450},
        {"term": "欧盟新规什么时候生效", "reason": "查的是当期政策资讯，内容很快过时", "source": "manual", "updatedAt": 440},
    ]
    ex = wordlib.pick_examples(rows)
    terms = [e["term"] for e in ex]
    assert "how to sound better" in terms
    assert "欧盟新规什么时候生效" in terms
    assert "some ai term" not in terms           # AI 判定不学习
    assert "foo" not in terms and "bar" not in terms  # 已固化理由不重复注入
    assert "baz" not in terms and "" not in terms


def test_pick_examples_dedup_by_reason_and_limit():
    """同一理由只留最新一条；limit 生效；上限 20 封顶。"""
    rows = [
        {"term": f"old-{i}", "reason": "查的是一次性资讯不是工具", "source": "manual", "updatedAt": 100 + i}
        for i in range(5)
    ] + [
        {"term": f"uniq-{i}", "reason": f"第{i}种独立理由说明", "source": "manual", "updatedAt": 200 + i}
        for i in range(10)
    ]
    # 取足够多时验证「同理由只留一条」：5 条 old 同理由 → 只剩 1 条
    all_ex = wordlib.pick_examples(rows, limit=99)
    assert sum(1 for e in all_ex if e["term"].startswith("old-")) == 1
    assert sum(1 for e in all_ex if e["term"].startswith("uniq-")) == 10
    # limit 生效
    assert len(wordlib.pick_examples(rows, limit=3)) == 3
    assert len(all_ex) <= 20


def test_pick_examples_handles_bad_input():
    assert wordlib.pick_examples(None) == []
    assert wordlib.pick_examples("not a list") == []
    assert wordlib.pick_examples([None, 3, "x"]) == []


def test_examples_line_format():
    assert hm.examples_line([]) == ""
    line = hm.examples_line([{"term": "abc", "reason": "理由一"}])
    assert "过去判定「不做」" in line and "abc — 理由一" in line


def test_verdict_of():
    assert verdict_of("1) 词义…\n7) 结论：做，理由") == "做"
    assert verdict_of("7) 结论: 观望，等长尾") == "观望"
    assert verdict_of("8) 结论：不做，人名") == "不做"
    assert verdict_of("结论：观望但明显不做") == "不做"   # 小节号变了也能兜住
    assert verdict_of("没有结论小节") == ""


def test_redline_of():
    assert "姓名权" in redline_of("这是真人姓名，有侵权风险")
    assert "版权" in redline_of("这是一部电影，涉版权")
    assert redline_of("看着像电影但其实不是") == ""       # 不能误杀


def test_work_signal_needs_two_hits():
    two = [{"title": "Foo (2026 film)", "domain": "imdb.com"},
           {"title": "Foo box office", "domain": " variety.com"}]
    assert "具体作品" in work_signal(two)
    assert work_signal(two[:1]) == ""
    assert work_signal([]) == ""


def test_signals_of():
    assert signals_of("8) 信号标签：工具查询、日程日期") == ["工具查询", "日程日期"]
    assert signals_of("8) 信号标签：无") == []
    assert signals_of("没有第 8 节") is None
    assert len(signals_of("8) 信号标签：工具查询、价格规格、日程日期、资格政策")) == 3


def test_decide_priority():
    assert decide("7) 结论：做，工具站") == ("做", "")
    assert decide("7) 结论：观望") == ("观望", "")
    assert decide("7) 结论：不做") == ("不做", "")
    verdict, reason = decide("7) 结论：观望，但是真人姓名有侵权风险")
    assert verdict == "不做" and "姓名权" in reason
    verdict, reason = decide(
        "7) 结论：做",
        "SERP 标题显示这是具体作品（Foo (2026 film)）")
    assert verdict == "不做" and "具体作品" in reason


def test_serp_brief_fallback_to_domains():
    assert serp_brief({"items": [{"domain": "a.com", "title": "A"}]}) == [
        {"domain": "a.com", "title": "A"}]
    assert serp_brief({"competition": {"domains": ["b.com"]}}) == [
        {"domain": "b.com", "title": ""}]
    assert serp_brief(None) == []


def test_analyze_hotwords_is_a_line_not_a_tool():
    """两条线都是编排：编排本身不进 registry，编排调用的 IO 才是工具。

    2026-11-20 分层归位：analyze_hotwords 从 tools 移除，由独立的 `POST /hotwords`
    触发 graph 的 run_hotwords；热词线的逐词 IO 与常规线一样按名字表经 tool_service
    分发到 registry（execute_for_intent / execute_for_step）。
    """
    assert "analyze_hotwords" not in tool_registry.names()
    assert "analyze_hotwords" not in INTENT_TOOLS
    assert "get_hotwords" in INTENT_TOOLS[Intent.WORD_PLAN_PAGE.value]
    assert set(STEP_TOOLS) == {s.value for s in HotwordStep}   # 步骤名口径唯一
    for step, name in STEP_TOOLS.items():
        assert name in tool_registry.names(), f"步骤 {step} 路由到不存在的工具 {name}"


# ---------------------------------------------------------------------------
# run_hotwords 经共享 KV 的端到端写回（离线：外部抓取 + LLM 全部 monkeypatch）
# ---------------------------------------------------------------------------

def _seed_candidate(fake_d1, term, *, score=80, hints=None):
    term_id = fake_d1.seed_term(term, hints=hints or ["工具查询"], related=["related query"])
    fake_d1.seed_candidate(term_id, score=score, hints=hints or ["工具查询"],
                           geos=["US", "GB"], related=["related query"],
                           traffic=900, geo_count=2, survival_rounds=3)
    return term_id


def test_snapshot_reads_active_candidate_from_d1(fake_d1):
    term_id = _seed_candidate(fake_d1, "free invoice calculator")
    fake_d1.seed_run(metadata={"raw": 100, "kept": 80})
    snap = wordlib.snapshot()
    assert [item["term"] for item in snap["items"]] == ["free invoice calculator"]
    assert snap["items"][0]["key"] == "free invoice calculator"
    assert snap["items"][0]["geoCount"] == 2
    assert snap["meta"]["runId"] == "1"


def test_snapshot_filters_manual_state_and_reads_follow_up_status(fake_d1):
    active = _seed_candidate(fake_d1, "active calculator")
    saved = _seed_candidate(fake_d1, "saved calculator")
    fake_d1.seed_status(saved, is_favorite=1, status="watching")
    assert [item["term"] for item in wordlib.snapshot()["items"]] == ["active calculator"]
    saved_items = wordlib.snapshot(status=CurationStatus.SAVED.value)["items"]
    assert [item["term"] for item in saved_items] == ["saved calculator"]


def test_select_pending_excludes_completed_and_curated_terms(fake_d1):
    done = _seed_candidate(fake_d1, "done calculator", score=90)
    todo = _seed_candidate(fake_d1, "todo calculator", score=80)
    tagged = _seed_candidate(fake_d1, "tagged calculator", score=70)
    rejected = _seed_candidate(fake_d1, "rejected calculator", score=60)
    pending_term = _seed_candidate(fake_d1, "pending calculator", score=50)
    fake_d1.seed_analysis(done, text="already analysed")
    fake_d1.seed_status(todo, status="todo")
    fake_d1.seed_status(tagged, tag="tool")
    fake_d1.seed_status(rejected, rejected_at="2026-11-28T00:00:00Z",
                        rejected_reason="not a tool query", rejected_source="manual")
    pending = wordlib.select_pending(set())
    assert pending["term"] == "pending calculator"
    assert wordlib.select_pending({pending["key"]}) is None


def test_select_pending_returns_one_and_respects_seen(fake_d1):
    _seed_candidate(fake_d1, "first calculator", score=90)
    _seed_candidate(fake_d1, "second calculator", score=80)
    first = wordlib.select_pending(set())
    assert first["term"] == "first calculator"
    assert wordlib.select_pending({first["key"]})["term"] == "second calculator"
    assert wordlib.select_pending({first["key"], "second calculator"}) is None


def test_write_result_persists_analysis_and_todo_status(fake_d1):
    term_id = fake_d1.seed_term("free invoice calculator")
    out = writeback.write_result(
        {"term": "free invoice calculator", "key": "free invoice calculator"},
        "1) 词义与意图\n这是一款计算工具。\n7) 结论：做",
        [HintName.TOOL_QUERY.value], Verdict.DO.value, "需求明确",
    )
    analysis = fake_d1.query("SELECT * FROM radar_term_analyses WHERE term_id = ?", [term_id])[0]
    status = fake_d1.query("SELECT * FROM radar_term_status WHERE term_id = ?", [term_id])[0]
    result = __import__("json").loads(analysis["result_json"])
    assert analysis["status"] == "completed"
    assert "这是一款计算工具" in result["text"]
    assert status["status"] == CurationStatus.TODO.value
    assert __import__("json").loads(status["hints_override_json"]) == [HintName.TOOL_QUERY.value]
    assert len(out["writes"]) == 2


def test_ai_skip_never_overwrites_manual_rejection(fake_d1):
    term_id = fake_d1.seed_term("manual reject term")
    fake_d1.seed_status(term_id, status="new", rejected_at="2026-11-01T00:00:00Z",
                        rejected_reason="人工确认不做", rejected_source=RejectSource.MANUAL.value)
    writeback.write_result(
        {"term": "manual reject term", "key": "manual reject term"},
        "1) 词义\n7) 结论：不做", [], Verdict.SKIP.value, "AI 不建议",
    )
    status = fake_d1.query("SELECT * FROM radar_term_status WHERE term_id = ?", [term_id])[0]
    assert status["rejected_source"] == RejectSource.MANUAL.value
    assert status["rejected_reason"] == "人工确认不做"


def test_serp_and_trend_cache_are_stored_in_d1(fake_d1, monkeypatch):
    _seed_candidate(fake_d1, "cache calculator")
    monkeypatch.setattr(fetch.scrape_serp, "fetch_serp", lambda term: {
        "engine": "duckduckgo", "items": [{"title": "Calculator", "domain": "example.com"}],
    })
    monkeypatch.setattr(fetch.scrape_serp, "score_competition", lambda items, term: {"level": "low"})
    monkeypatch.setattr(fetch.scrape_trend, "fetch_interest", lambda term: {
        "points": [{"date": "2026-11-01", "v": 70}], "summary": {"direction": "up"},
    })
    first = fetch.serp("cache calculator")
    second = fetch.serp("cache calculator")
    trend = fetch.trend("cache calculator")
    assert first["fromCache"] is False and second["fromCache"] is True
    assert trend["fromCache"] is False
    row = fake_d1.query(
        """SELECT serp_json, trend_json FROM radar_term_analyses
           WHERE term_id = (SELECT term_id FROM radar_terms WHERE term_key = ?)""",
        ["cache calculator"],
    )[0]
    assert __import__("json").loads(row["serp_json"])["competition"]["level"] == "low"
    assert __import__("json").loads(row["trend_json"])["summary"]["direction"] == "up"


def test_read_examples_uses_manual_rejections_only(fake_d1):
    manual = fake_d1.seed_term("manual example")
    ai = fake_d1.seed_term("ai example")
    fake_d1.seed_status(manual, rejected_at="2026-11-28T00:00:00Z",
                        rejected_reason="明确是娱乐资讯，不是工具", rejected_source="manual")
    fake_d1.seed_status(ai, rejected_at="2026-11-28T00:00:00Z",
                        rejected_reason="AI 判断不做", rejected_source="ai")
    examples = wordlib.read_examples()
    assert [item["term"] for item in examples] == ["manual example"]
