# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析：提示词拼装、结论/红线/标签判据、作品硬证据、意图→工具映射，以及
run_batch 经共享 KV 的端到端写回（不碰 console / radar / OpenAI / DDG / Google）。

2026-11-12 去接口化后，run_batch 直接读写共享 KV（见 sources/kv.py）。这里用
内存 FakeKV + monkeypatch 外部抓取/LLM，离线验证「候选 → 分析 → 回写状态」全链路。
"""

import pytest

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


# ---------------------------------------------------------------------------
# run_batch 经共享 KV 的端到端写回（离线：外部抓取 + LLM 全部 monkeypatch）
# ---------------------------------------------------------------------------
def _seed_candidates(fake_kv):
    fake_kv.put_json("candidates", [
        {"term": "free invoice calculator", "traffic": 900, "geos": ["US"], "geoCount": 1,
         "related": ["invoice calculator free"], "hints": [], "src": "trends", "count": 1, "score": 10},
        {"term": "Taylor Swift net worth", "traffic": 8000, "geos": ["US"], "geoCount": 1,
         "related": [], "hints": [], "src": "trends", "count": 1, "score": 99},
    ])


def _patch_external(monkeypatch):
    monkeypatch.setattr(hw.scrape_serp, "fetch_serp",
                        lambda term, session=None, limit=10: {
                            "engine": "duckduckgo",
                            "items": [{"url": "https://calc.example/x", "domain": "calc.example",
                                       "title": "Invoice Calculator"}],
                            "attempts": [],
                        })
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest",
                        lambda term, session=None, geo="": {
                            "points": [{"d": "2026-01-01", "v": 50}, {"d": "2026-01-02", "v": 60}],
                            "summary": {"avg": 55, "max": 60, "last": 60, "delta": 20, "direction": "up"},
                            "attempts": [],
                        })

    def fake_llm(messages):
        user = messages[-1]["content"]
        if "Taylor" in user:
            return "1) 真人 7) 结论：不做，真人姓名有侵权风险\n8) 信号标签：无", "fake-model"
        return "1) 工具站 7) 结论：做，工具站能吃长尾\n8) 信号标签：工具查询", "fake-model"

    monkeypatch.setattr(hw, "_llm", fake_llm)


def test_run_batch_writes_kv(fake_kv, monkeypatch):
    _seed_candidates(fake_kv)
    _patch_external(monkeypatch)

    out = hw.run_batch(limit=20, kv=fake_kv)
    assert out["ok"] is True
    assert out["fetched"] == 2 and out["analyzed"] == 2
    assert out["counts"].get("做") == 1 and out["counts"].get("不做") == 1

    # 做 → saved(status=todo)
    saved = fake_kv.get_json("saved", [])
    assert any(x["key"] == hw.term_key("free invoice calculator") and x["status"] == "todo" for x in saved)
    # 不做 → rejected(source=ai)
    rejected = fake_kv.get_json("rejected", [])
    assert any(x["key"] == hw.term_key("Taylor Swift net worth") and x["source"] == "ai" for x in rejected)
    # 正文回写 ai:v3:，标签回写 aihints
    assert fake_kv.get_text(hw.AI_CACHE_PREFIX + hw.term_key("free invoice calculator"))
    hints = fake_kv.get_json("aihints", {})
    assert hints.get(hw.term_key("free invoice calculator")) == ["工具查询"]
    # 竞争度 / 趋势摘要表被更新
    assert fake_kv.get_json("competition")
    assert fake_kv.get_json("interests")


def test_run_batch_serp_source_down_only_skips_serp(fake_kv, monkeypatch):
    """★2026-11-13：SERP 源挂不再中止整批，只让这批词跳过 SERP 照常出结论。

    仍要避免 12 个词各撞一遍 4 发超时（第一批并发发现后就置 serp_off）。
    """
    monkeypatch.setattr(hw, "SERP_GAP_SEC", 0)
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(12)])
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest",
                        lambda term, session=None, geo="": {
                            "points": [{"d": "2026-01-01", "v": 50}],
                            "summary": {"avg": 50}, "attempts": []})

    calls = {"n": 0}

    def boom(term, session=None, limit=10):
        calls["n"] += 1
        raise hw.scrape_serp.SourceUnavailable("duckduckgo", "连不上 DuckDuckGo", [])

    monkeypatch.setattr(hw.scrape_serp, "fetch_serp", boom)
    monkeypatch.setattr(hw, "_llm", lambda m: ("7) 结论：观望", "fake"))

    out = hw.run_batch(limit=20, kv=fake_kv)

    assert not out.get("aborted")            # 没有「整批中止」这回事了
    assert out["serpDisabled"] is True
    assert out["fetched"] == 12 and out["analyzed"] == 12
    assert calls["n"] <= hw.BATCH_WORKERS   # 发现源挂后本批不再撞 DDG
    assert all(r["serp"] is False for r in out["items"])
    assert any("SERP 未取到" in (r.get("notice") or "") for r in out["items"])
    assert len(fake_kv.list_keys(hw.AI_CACHE_PREFIX)) == 12


def test_run_batch_skips_serp_but_still_writes_states(fake_kv, monkeypatch):
    """SERP 降级只影响取数，不影响 LLM 结论与 saved/rejected 状态回写。"""
    monkeypatch.setattr(hw, "SERP_GAP_SEC", 0)
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(5)])
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest",
                        lambda term, session=None, geo="": {
                            "points": [{"d": "2026-01-01", "v": 50}],
                            "summary": {"avg": 50}, "attempts": []})
    calls = {"n": 0}

    def boom(term, session=None, limit=10):
        calls["n"] += 1
        raise hw.scrape_serp.SourceUnavailable("duckduckgo", "被挑战页占满", [])

    monkeypatch.setattr(hw.scrape_serp, "fetch_serp", boom)
    monkeypatch.setattr(hw, "_llm", lambda m: ("7) 结论：做，工具站\n8) 信号标签：工具查询", "fake"))

    out = hw.run_batch(limit=20, kv=fake_kv)

    assert not out.get("aborted")
    assert out["analyzed"] == 5
    assert calls["n"] <= hw.BATCH_WORKERS
    assert len(fake_kv.get_json("saved", [])) == 5
    assert all(r["status"] == "todo" for r in out["items"])


def test_run_batch_keeps_going_when_trends_down(fake_kv, monkeypatch):
    """趋势只是辅助证据：源不可用时本批禁用，但分析继续（不能因它整批白跑）。"""
    monkeypatch.setattr(hw, "SERP_GAP_SEC", 0)
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(5)])
    monkeypatch.setattr(hw.scrape_serp, "fetch_serp",
                        lambda term, session=None, limit=10: {
                            "engine": "duckduckgo", "items": [], "attempts": []})

    trend_calls = {"n": 0}

    def trends_down(term, session=None, geo=""):
        trend_calls["n"] += 1
        raise hw.scrape_trend.SourceUnavailable("google-trends", "连不上 Google Trends", [])

    monkeypatch.setattr(hw.scrape_trend, "fetch_interest", trends_down)
    monkeypatch.setattr(hw, "_llm", lambda m: ("7) 结论：观望", "fake"))

    out = hw.run_batch(limit=20, kv=fake_kv)

    assert not out.get("aborted")
    assert out["fetched"] == 5
    assert out["analyzed"] == 5
    # 只撞一次超时就把趋势关掉，不是 5 个词各撞一次
    assert trend_calls["n"] <= hw.BATCH_WORKERS
    assert len(fake_kv.list_keys(hw.AI_CACHE_PREFIX)) == 5


def test_run_batch_llm_failures_are_word_local(fake_kv, monkeypatch):
    """★LLM 连败/单词失败只记该词 error，其它词照常跑完并回写状态。"""
    monkeypatch.setattr(hw, "SERP_GAP_SEC", 0)
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(12)])
    monkeypatch.setattr(hw.scrape_serp, "fetch_serp",
                        lambda term, session=None, limit=10: {
                            "engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest",
                        lambda term, session=None, geo="": {
                            "points": [{"d": "2026-01-01", "v": 50}],
                            "summary": {"avg": 50}, "attempts": []})

    def llm(messages):
        if "term 0" in messages[-1]["content"]:
            raise RuntimeError("LLM 连接超时")
        return "7) 结论：做，工具站", "fake"

    monkeypatch.setattr(hw, "_llm", llm)

    out = hw.run_batch(limit=20, kv=fake_kv)

    assert not out.get("aborted")
    assert out["llmFailures"] == 1
    assert out["fetched"] == 12 and out["analyzed"] == 11 and out["failed"] == 1
    assert "LLM 调用失败" in out["items"][0]["error"]
    assert len(fake_kv.list_keys(hw.AI_CACHE_PREFIX)) == 11
    assert len(fake_kv.get_json("saved", [])) == 11


def test_run_batch_no_candidates(fake_kv):
    out = hw.run_batch(limit=20, kv=fake_kv)
    assert out["ok"] is True and out["fetched"] == 0
    assert "没有未跟进" in (out.get("note") or "")


# ---- 2026-11-13：DeepSeek 上线后的三处修复（防止悄悄退化） ----

def test_llm_uses_shared_client_and_normalizes_dict(monkeypatch, caplog):
    """逐词 LLM 必须走 get_llm()（含 trace/HTTP 钩子），且能归一 wrap_openai 的 dict 形态。

    此前裸建 OpenAI 客户端 + 裸写 resp.choices[0].message.content，既不进 trace，
    遇到 dict 响应会直接抛 AttributeError 把整条算成失败。
    """
    import logging
    import src.app.llm.provider as provider

    calls = {}

    def fake_create(**kwargs):
        calls.update(kwargs)
        return {"choices": [{"message": {"role": "assistant", "content": "7) 结论：观望"},
                             "finish_reason": "stop"}]}

    class FakeCompletions:
        create = staticmethod(fake_create)

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    # _llm 内部是延迟 import，patch 源模块 config 才生效
    monkeypatch.setattr(provider, "get_llm", lambda: FakeClient())
    monkeypatch.setattr("src.app.config.LLM_API_KEY", "sk-test")
    monkeypatch.setattr("src.app.config.LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr("src.app.config.LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr("src.app.config.LLM_THINKING", "auto")

    with caplog.at_level(logging.WARNING):
        text, model = hw._llm([{"role": "user", "content": "x"}])

    assert text == "7) 结论：观望"
    assert model == "deepseek-flash"
    assert calls["max_tokens"] == hw.MAX_TOKENS == 2000  # 不再被推理 token 吃光预算
    assert calls["temperature"] == 0 and calls["timeout"] == 60
    # DeepSeek 思考模式默认开，逐词分析必须显式关掉，否则 2000 全被 reasoning 吃掉
    assert calls["extra_body"] == {"thinking": {"type": "disabled"}}


def test_llm_empty_content_reports_real_cause(monkeypatch, caplog):
    """线上真实响应：finish_reason=length、content=""、reasoning_tokens=2000。

    此前 _analyze_one 一律报「LLM_API_KEY 未配置或返回为空」，与现场对不上。
    现在必须指向「输出预算耗尽」，并带全 finish_reason / reasoning_tokens。
    """
    import logging
    import src.app.llm.provider as provider

    class FakeCompletions:
        @staticmethod
        def create(**_kwargs):
            return {
                "model": "deepseek-flash",
                "choices": [{"index": 0, "finish_reason": "length",
                             "message": {"role": "assistant", "content": "",
                                         "reasoning_content": "We need answer..."}}],
                "usage": {"completion_tokens": 2000, "prompt_tokens": 1194,
                          "completion_tokens_details": {"reasoning_tokens": 2000}},
            }

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(provider, "get_llm", lambda: FakeClient())
    monkeypatch.setattr("src.app.config.LLM_API_KEY", "sk-test")
    monkeypatch.setattr("src.app.config.LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr("src.app.config.LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr("src.app.config.LLM_THINKING", "auto")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(hw.LLMEmptyContent) as ei:
            hw._llm([{"role": "user", "content": "x"}])

    diag = ei.value.diag
    assert diag["finish_reason"] == "length"
    assert diag["reasoning_tokens"] == 2000
    assert diag["max_tokens"] == 2000
    assert "预算耗尽" in diag["reason"]
    assert "LLM_API_KEY" not in str(ei.value)      # 绝不能误导成 key 没配
    msg = next(r.getMessage() for r in caplog.records
               if "hotwords_llm_empty_content" in r.message)
    assert "finish_reason=length" in msg and "reasoning_tokens=2000" in msg


def test_empty_content_without_reasoning_blames_text_budget(monkeypatch):
    """思考已关、正文自己超预算时，不能把原因写成「推理吃光预算」。"""
    import src.app.llm.provider as provider

    class FakeCompletions:
        @staticmethod
        def create(**_kwargs):
            return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                    "usage": {"completion_tokens": 2000,
                              "completion_tokens_details": {"reasoning_tokens": 0}}}

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(provider, "get_llm", lambda: FakeClient())
    monkeypatch.setattr("src.app.config.LLM_API_KEY", "sk-test")
    with pytest.raises(hw.LLMEmptyContent) as ei:
        hw._llm([{"role": "user", "content": "x"}])
    assert "正文超出 MAX_TOKENS" in ei.value.diag["reason"]
    assert "思考推理" not in ei.value.diag["reason"]
    assert "调大 MAX_TOKENS" in ei.value.diag["hint"]


def test_analyze_one_error_matches_real_cause(fake_kv, monkeypatch):
    """整批 items[].error 要带真实诊断，且空正文时绝不回写状态。"""
    term = "ato unpaid super recovery"
    fake_kv.put_json("candidates", [{"term": term}])
    fake_kv.put_json(hw.SERP_PREFIX + term,
                     {"items": [], "competition": {"score": 1, "level": "low"}})
    fake_kv.put_json(hw.TREND_PREFIX + term, {"points": [], "summary": ""})

    diag = {"reason": "输出预算耗尽（思考推理吃光 max_tokens，正文未生成）",
            "finish_reason": "length", "reasoning_tokens": 2000,
            "max_tokens": 2000}

    def fake_llm(_messages):
        raise hw.LLMEmptyContent("LLM 返回空正文：输出预算耗尽；finish_reason=length", diag)

    monkeypatch.setattr(hw, "_llm", fake_llm)
    out = hw.run_batch(limit=1, kv=fake_kv)
    item = out["items"][0]
    assert "预算耗尽" in item["error"]
    assert "LLM_API_KEY 未配置" not in item["error"]
    assert item["llm_diag"]["finish_reason"] == "length"
    assert out["analyzed"] == 0
    assert fake_kv.get_json("rejected", []) == []       # 不编正文、不写状态
    assert fake_kv.get_json("saved", []) == []


def test_no_key_error_message(monkeypatch):
    """没配 key 时才报 key 未配置；且不应发起任何 LLM 请求。"""
    import src.app.llm.provider as provider

    monkeypatch.setattr(provider, "get_llm", lambda: 1 / 0)   # 被调用就炸
    monkeypatch.setattr("src.app.config.LLM_API_KEY", "")
    with pytest.raises(hw.LLMEmptyContent) as ei:
        hw._llm([{"role": "user", "content": "x"}])
    assert "LLM_API_KEY" in str(ei.value)
    assert ei.value.diag["no_key"] is True


def test_thinking_kwargs_scope(monkeypatch):
    """auto 只作用于 DeepSeek，换 provider 不误传专属参数；on/off 可显式覆盖。"""
    monkeypatch.setattr("src.app.config.LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setattr("src.app.config.LLM_THINKING", "auto")
    assert hw._thinking_kwargs() == {}
    monkeypatch.setattr("src.app.config.LLM_THINKING", "on")
    assert hw._thinking_kwargs() == {}
    monkeypatch.setattr("src.app.config.LLM_THINKING", "off")
    assert hw._thinking_kwargs() == {"extra_body": {"thinking": {"type": "disabled"}}}
    monkeypatch.setattr("src.app.config.LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr("src.app.config.LLM_THINKING", "auto")
    assert hw._thinking_kwargs() == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_run_batch_ok_requires_success_rate(fake_kv, monkeypatch):
    """整批 ok 按成功率判定：11 失败 / 1 成功不能再被报成 success。"""
    monkeypatch.setattr(hw, "SERP_GAP_SEC", 0)
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(4)])
    monkeypatch.setattr(hw.scrape_serp, "fetch_serp",
                        lambda term, session=None, limit=10: {
                            "engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest",
                        lambda term, session=None, geo="": {
                            "points": [{"d": "2026-01-01", "v": 50}],
                            "summary": {"avg": 50}, "attempts": []})

    def one_success(messages):
        user = messages[-1]["content"]
        if "term 0" in user:
            return "7) 结论：观望", "fake"
        return "", "fake"   # 空正文 → LLM 无正文

    monkeypatch.setattr(hw, "_llm", one_success)

    out = hw.run_batch(limit=20, kv=fake_kv)
    assert out["analyzed"] == 1 and out["failed"] == 3
    assert out["successRate"] == 0.25
    assert out["ok"] is False   # 旧口径 ok>0 会错报 True


def test_select_pending_excludes_curated(fake_kv):
    fake_kv.put_json("candidates", [
        {"term": "a"}, {"term": "b"}, {"term": "c"}, {"term": "d"},
    ])
    fake_kv.put_json("saved", [{"key": hw.term_key("a"), "term": "a", "status": "saved"}])
    fake_kv.put_json("rejected", [{"key": hw.term_key("b"), "term": "b", "status": "rejected"}])
    fake_kv.put_json("tags", {hw.term_key("c"): ["工具查询"]})
    fake_kv.put_text(hw.AI_CACHE_PREFIX + hw.term_key("d"), "already analyzed")
    pending = hw._select_pending(fake_kv, 20)
    assert pending == []  # 已收藏 / 不做 / 打标签 / 已分析 全部剔除


def test_select_pending_limit_and_order(fake_kv):
    fake_kv.put_json("candidates", [{"term": f"t{i}"} for i in range(10)])
    pending = hw._select_pending(fake_kv, 3)
    assert [p["term"] for p in pending] == ["t0", "t1", "t2"]


def test_select_pending_survives_shape_drift(fake_kv):
    """★KV 表形状漂移不能整批失败（线上踩过 'list' object has no attribute 'get'）。"""
    fake_kv.put_json("candidates", [{"term": "a"}, {"term": "b"}])
    fake_kv.put_json("tags", ["不是对象而是数组"])          # 期望 dict，实际 list
    fake_kv.put_json("saved", {"a": {"status": "saved"}})   # 期望 list，实际 dict
    fake_kv.put_json("rejected", "垃圾")
    pending = hw._select_pending(fake_kv, 20)  # 不抛
    assert [p["term"] for p in pending] == ["a", "b"]  # 形状坏的当空，不误剔除

    fake_kv.put_json("candidates", {"a": 1})  # 期望 list，实际 dict
    assert hw._select_pending(fake_kv, 20) == []


def test_analyze_one_skips_blank(fake_kv):
    out = hw._analyze_one({"term": ""}, fake_kv)
    assert out["error"] == "空词"
    # 不写任何 KV
    assert fake_kv.writes == 0


def test_run_batch_kv_unreachable_returns_error(fake_kv, monkeypatch):
    class BoomKV:
        def get_json(self, *a, **k):
            raise RuntimeError("unreachable")
    out = hw.run_batch(limit=20, kv=BoomKV())
    assert out["ok"] is False and "候选池读取失败" in out["error"]
    assert out["fetched"] == 0  # 没读到候选，不写任何状态


# ---- 2026-11-13：整表原子写 / 写失败记 error / 状态回填 ----

def _patch_ok_sources(monkeypatch):
    monkeypatch.setattr(hw, "SERP_GAP_SEC", 0)
    monkeypatch.setattr(hw.scrape_serp, "fetch_serp",
                        lambda term, session=None, limit=10: {
                            "engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest",
                        lambda term, session=None, geo="": {
                            "points": [{"d": "2026-01-01", "v": 50}],
                            "summary": {"avg": 50}, "attempts": []})


def test_status_table_write_failure_marks_error_and_is_backfillable(fake_kv, monkeypatch):
    """saved 整表写失败 → 该词 error（不算 analyzed），但正文已落库不会重跑；
    backfill_states 从正文把状态补回去。"""
    term = "free invoice calculator"
    fake_kv.put_json("candidates", [{"term": term, "traffic": 900, "geoCount": 1}])
    _patch_ok_sources(monkeypatch)
    monkeypatch.setattr(hw, "_llm",
                        lambda m: ("7) 结论：做，工具站\n8) 信号标签：工具查询", "fake"))
    fake_kv.fail_keys.add(hw.SAVED_KEY)

    out = hw.run_batch(limit=1, kv=fake_kv)
    item = out["items"][0]
    assert "KV 写入失败" in item["error"] and hw.SAVED_KEY in item["writeErrors"]
    assert out["analyzed"] == 0
    assert fake_kv.get_json("saved", []) == []
    assert fake_kv.get_text(hw.AI_CACHE_PREFIX + hw.term_key(term))  # 正文在，状态丢了

    # 模拟瞬时故障恢复后跑回填
    fake_kv.fail_keys.clear()
    dry = hw.backfill_states(kv=fake_kv, dry_run=True)
    assert dry["dryRun"] is True and dry["repaired"] == 0
    assert any(i["key"] == hw.term_key(term) and i["action"] == "preview" for i in dry["items"])
    assert fake_kv.get_json("saved", []) == []

    rep = hw.backfill_states(kv=fake_kv)
    assert rep["ok"] is True and rep["repaired"] == 1
    saved = fake_kv.get_json("saved", [])
    assert any(x["key"] == hw.term_key(term) and x["status"] == "todo" for x in saved)


def test_body_write_failure_never_writes_state(fake_kv, monkeypatch):
    """正文写失败就收手：绝不留下「有状态、没正文」的悬空词，且该词仍可重跑。"""
    term = "free invoice calculator"
    key = hw.AI_CACHE_PREFIX + hw.term_key(term)
    fake_kv.put_json("candidates", [{"term": term}])
    _patch_ok_sources(monkeypatch)
    monkeypatch.setattr(hw, "_llm",
                        lambda m: ("7) 结论：做，工具站\n8) 信号标签：工具查询", "fake"))
    fake_kv.fail_keys.add(key)

    out = hw.run_batch(limit=1, kv=fake_kv)
    assert "KV 写入失败" in out["items"][0]["error"]
    assert out["analyzed"] == 0
    assert fake_kv.get_json("saved", []) == []
    assert fake_kv.get_json("rejected", []) == []
    assert fake_kv.get_json("aihints", {}) == {}
    assert fake_kv.get_text(key) is None
    assert len(hw._select_pending(fake_kv, 20)) == 1   # 下次还会重跑


def test_concurrent_table_updates_do_not_lose_rows(fake_kv, monkeypatch):
    """★回归：4 个 worker 并发「读-改-写」5 张整表不能互相覆盖。

    故意给 get/put 加延时放大竞态：没有 kv.atomic_update 的全局 RLock 时，
    saved/rejected/aihints/competition/interests 会只留下最后写入的那一条。
    """
    import time

    orig_get, orig_put = fake_kv.get_json, fake_kv.put_json

    def slow_get(*a, **k):
        time.sleep(0.002)
        return orig_get(*a, **k)

    def slow_put(*a, **k):
        time.sleep(0.002)
        return orig_put(*a, **k)

    monkeypatch.setattr(fake_kv, "get_json", slow_get)
    monkeypatch.setattr(fake_kv, "put_json", slow_put)
    n = 12
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(n)])
    _patch_ok_sources(monkeypatch)

    def llm(messages):
        term = messages[-1]["content"].split("关键词：")[1].split("\n")[0]
        do_it = int(term.split()[-1]) % 2 == 0
        return (f"7) 结论：{'做' if do_it else '不做'}，工具站\n8) 信号标签：工具查询", "fake")

    monkeypatch.setattr(hw, "_llm", llm)
    out = hw.run_batch(limit=20, kv=fake_kv)

    assert out["analyzed"] == n
    assert len(fake_kv.list_keys(hw.AI_CACHE_PREFIX)) == n
    assert len(fake_kv.get_json("saved", [])) + len(fake_kv.get_json("rejected", [])) == n
    assert len(fake_kv.get_json("aihints", {})) == n
    assert len(fake_kv.get_json("competition", {})) == n
    assert len(fake_kv.get_json("interests", {})) == n


def test_backfill_states_dry_run_and_unresolved(fake_kv):
    """预览不写库；结论解析不出的词列成 unresolved（要重跑得先删 ai:v3: 正文）。"""
    fake_kv.put_text("ai:v3:foo", "7) 结论：不做，真人姓名\n8) 信号标签：工具查询")
    fake_kv.put_text("ai:v3:bar", "7) 结论：观望，等长尾\n8) 信号标签：无")
    fake_kv.put_text("ai:v3:baz", "没有结论小节的乱码")

    dry = hw.backfill_states(kv=fake_kv, dry_run=True)
    assert dry["dryRun"] is True and dry["scanned"] == 3
    assert fake_kv.get_json("rejected", []) == []
    assert any(i["key"] == "foo" and i["action"] == "preview" for i in dry["items"])
    assert any(i["key"] == "baz" and i["action"] == "unresolved" for i in dry["items"])

    rep = hw.backfill_states(kv=fake_kv)
    assert rep["ok"] is True and rep["repaired"] == 1
    assert any(x["key"] == "foo" and x["source"] == "ai"
               for x in fake_kv.get_json("rejected", []))
    assert fake_kv.get_json("aihints", {}).get("foo") == ["工具查询"]
    assert "bar" not in fake_kv.get_json("aihints", {})   # 「无」不写空覆盖


def test_backfill_states_skips_already_curated(fake_kv):
    """已整理 / 人工打标的词不能被回填改写，重复跑也要幂等。"""
    fake_kv.put_text("ai:v3:foo", "7) 结论：不做，真人姓名\n8) 信号标签：工具查询")
    fake_kv.put_json("saved", [{"key": "foo", "term": "foo", "status": "wip"}])
    rep = hw.backfill_states(kv=fake_kv)
    assert rep["repaired"] == 0 and rep["hintsRepaired"] == 1
    assert fake_kv.get_json("saved", [])[0]["status"] == "wip"
    assert fake_kv.get_json("rejected", []) == []
