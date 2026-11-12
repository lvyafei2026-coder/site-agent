# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析：提示词拼装、结论/红线/标签判据、作品硬证据、意图→工具映射。

全部是纯函数 + FakeRuntime，不碰 console / radar / OpenAI。
"""

from src.app.tools.adapters.sources import hotwords as hw
from src.app.services.tool_service import INTENT_TOOLS, tool_service
from src.app.tools.registry import tool_registry


def test_build_messages_keeps_serp_titles():
    msgs = hw.build_messages({
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
    user = hw.build_messages({"term": "x", "related": [], "hints": []})[1]["content"]
    assert "关联查询：（无）" in user
    assert "现有搜索结果" not in user
    assert "未命中明显常青信号" in user


def test_verdict_of():
    assert hw.verdict_of("1) 词义…\n7) 结论：做，理由") == "做"
    assert hw.verdict_of("7) 结论: 观望，等长尾") == "观望"
    assert hw.verdict_of("8) 结论：不做，人名") == "不做"
    assert hw.verdict_of("结论：观望但明显不做") == "不做"   # 小节号变了也能兜住
    assert hw.verdict_of("没有结论小节") == ""


def test_redline_of():
    assert "姓名权" in hw.redline_of("这是真人姓名，有侵权风险")
    assert "版权" in hw.redline_of("这是一部电影，涉版权")
    assert hw.redline_of("看着像电影但其实不是") == ""       # 不能误杀


def test_work_signal_needs_two_hits():
    two = [{"title": "Foo (2026 film)", "domain": "imdb.com"},
           {"title": "Foo box office", "domain": " variety.com"}]
    assert "具体作品" in hw.work_signal(two)
    assert hw.work_signal(two[:1]) == ""
    assert hw.work_signal([]) == ""


def test_signals_of():
    assert hw.signals_of("8) 信号标签：工具查询、日程日期") == ["工具查询", "日程日期"]
    assert hw.signals_of("8) 信号标签：无") == []
    assert hw.signals_of("没有第 8 节") is None
    assert len(hw.signals_of("8) 信号标签：工具查询、价格规格、日程日期、资格政策")) == 3


def test_decide_priority():
    assert hw.decide("7) 结论：做，工具站") == ("做", "")
    assert hw.decide("7) 结论：观望") == ("观望", "")
    assert hw.decide("7) 结论：不做") == ("不做", "")
    verdict, reason = hw.decide("7) 结论：观望，但是真人姓名有侵权风险")
    assert verdict == "不做" and "姓名权" in reason
    verdict, reason = hw.decide(
        "7) 结论：做",
        "SERP 标题显示这是具体作品（Foo (2026 film)）")
    assert verdict == "不做" and "具体作品" in reason


def test_serp_brief_fallback_to_domains():
    assert hw.serp_brief({"items": [{"domain": "a.com", "title": "A"}]}) == [
        {"domain": "a.com", "title": "A"}]
    assert hw.serp_brief({"competition": {"domains": ["b.com"]}}) == [
        {"domain": "b.com", "title": ""}]
    assert hw.serp_brief(None) == []


def test_intent_maps_to_analyze_tool():
    assert INTENT_TOOLS["analyze_hotwords"] == ("analyze_hotwords",)
    assert "analyze_hotwords" in tool_registry.names()


def test_run_tool_uses_injected_runtime(fake):
    out = tool_service.execute_for_intent("analyze_hotwords", "分析未跟进热词", {})
    assert set(out) == {"analyze_hotwords"}
    result = out["analyze_hotwords"]["result"]
    assert result["analyzed"] == 2
    assert result["counts"] == {"做": 1, "不做": 1}
    assert result["items"][0]["status"] == "todo"
    assert result["items"][1]["status"] == "rejected"
    assert fake.counts["analyze_hotwords"] == 1
