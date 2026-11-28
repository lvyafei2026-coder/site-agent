# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""排产服务的落库契约：入选词必须变成 planned 台账行，不然建页线拿不到活。"""

import src.app.services.planning_service as planning_mod
from src.app.services.planning_service import (
    intent_from_analysis, planning_service, render_report, suggest_new_sites,
    term_intent, valid_excluded, valid_items, valid_new_sites,
)


def _json(data):
    """complete_json 的返回形状：(data, failed, retryable)。"""
    return lambda *a, **k: (data, False, False)


def _rows(written):
    return [item["row"] for item in written]


def _capture(monkeypatch):
    written = []
    monkeypatch.setattr(planning_mod._ledger, "ledger_upsert",
                        lambda table, row: written.append({"table": table, "row": row}))
    return written


def test_plan_saves_planned_rows_and_renders_report(monkeypatch):
    """结构化输出 → 入选词按顺序写 planned 行，报告由代码渲染并与台账一致。"""
    written = _capture(monkeypatch)
    monkeypatch.setattr(planning_mod, "complete_json", _json({
        "items": [{"term": "blackout", "site": "clocksgoback", "priority": "高",
                   "reason": "停电查询"},
                  {"term": "full moon", "site": "invoices", "priority": "中",
                   "reason": "月相"}],
        "excluded": [{"term": "tax form", "reason": "无匹配站点"}],
    }))

    out = planning_service.plan({"get_hotwords": {"ok": True, "result": {}}})

    assert "| 高 | blackout | clocksgoback | 停电查询 |" in out
    assert "**tax form** — 无匹配站点" in out
    # 2 个入选 + 1 个建议新站（tax form）都进待建页
    assert "已写入待建页 3/3 条 planned 行" in out
    assert [r["keyword"] for r in _rows(written)] == ["blackout", "full moon", "tax form"]
    assert all(r["status"] == "planned" for r in _rows(written))
    assert _rows(written)[0]["site"] == "clocksgoback"
    assert _rows(written)[2]["site"] == "tax-form"      # 建议新站用派生站名


def test_plan_drops_unknown_site(monkeypatch):
    """site 不在站群清单里的条目丢掉（LLM 会编站名）；一条都不合法时不写库。"""
    written = _capture(monkeypatch)
    monkeypatch.setattr(planning_mod, "complete_json", _json({
        "items": [{"term": "blackout", "site": "site-that-does-not-exist"}],
    }))

    assert "本轮无可排产词" in planning_service.plan({})
    assert written == []


def test_plan_falls_back_to_free_text_without_saving(monkeypatch):
    """拿不到结构化输出：退回纯文本问一次，报告照常展示，这一轮不写库。"""
    written = _capture(monkeypatch)
    monkeypatch.setattr(planning_mod, "complete_json", _json(None))
    monkeypatch.setattr(planning_mod, "complete_text", lambda *a, **k: "## 本轮排产\n手写报告")

    assert planning_service.plan({}) == "## 本轮排产\n手写报告"
    assert written == []


def test_valid_items_normalizes_optional_fields():
    assert valid_items([
        {"term": " blackout ", "site": "clocksgoback", "priority": "高", "reason": " 停电 "},
        {"term": "", "site": "clocksgoback"},
        {"term": "x", "site": "nope"},
        "not a dict",
    ]) == [{"term": "blackout", "site": "clocksgoback",
            "priority": "高", "reason": "停电"}]


def test_render_report_marks_empty_plan():
    """没有入选词时给明确结论，不给空表格。"""
    assert "本轮无可排产词" in render_report([], [{"term": "x", "reason": "y"}])


def test_valid_excluded_keeps_reason_only():
    assert valid_excluded([{"term": " tax ", "reason": " 无站 "}, {"reason": "缺词"}]) == \
        [{"term": "tax", "reason": "无站"}]


def test_plan_syncs_landed_terms_to_wip(monkeypatch):
    """落 planned 行的词同步进热词状态闭环；台账写失败的词不带上。"""
    written = _capture(monkeypatch)
    seen = []
    monkeypatch.setattr(planning_mod.term_state_service, "after_plan",
                        lambda terms: seen.extend(terms))
    monkeypatch.setattr(planning_mod, "complete_json", _json({
        "items": [{"term": "blackout", "site": "clocksgoback"},
                  {"term": "full moon", "site": "clocksgoback"}],
        "excluded": [],
    }))

    planning_service.plan({"get_hotwords": {"ok": True, "result": {}}})
    assert seen == ["blackout", "full moon"]
    assert len(written) == 2


def test_plan_shows_model_prose_without_saving(monkeypatch):
    """模型把正文塞在 content 里没给数组：正文照出，但明确标注未落台账。"""
    written = _capture(monkeypatch)
    monkeypatch.setattr(planning_mod, "complete_json", _json(
        {"type": "text", "content": "## 本轮排产\n建议先做 blackout"}))

    out = planning_service.plan({})
    assert "建议先做 blackout" in out
    assert "未落台账" in out
    assert written == []


def test_suggest_new_sites_derives_from_excluded():
    """排除词按「站名即子域名」派生候选站；品牌/理由一并带出。"""
    out = suggest_new_sites([
        {"term": "medicare", "reason": "无匹配站点"},
        {"term": "is market open today", "reason": "无匹配站点"},
    ])
    assert out == [
        {"term": "medicare", "name": "medicare", "brand": "Medicare",
         "domain": "medicare.toolara.dev", "reason": "无匹配站点"},
        {"term": "is market open today", "name": "is-market-open-today",
         "brand": "Is Market Open Today", "domain": "is-market-open-today.toolara.dev",
         "reason": "无匹配站点"},
    ]


def test_suggest_new_sites_skips_existing_and_dedupes():
    """已在站群清单的站名不再建议；同名词/同派生名只保留一个。"""
    out = suggest_new_sites([
        {"term": "music", "reason": "无匹配站点"},
        {"term": "medicare", "reason": "无匹配站点"},
        {"term": "Medicare", "reason": "同义合并"},
    ])
    assert [s["name"] for s in out] == ["medicare"]


def test_suggest_new_sites_model_proposal_overrides():
    """模型提案按 term 覆盖默认派生（站名已由 valid_new_sites 校验过）。"""
    proposals = valid_new_sites([
        {"term": "medicare", "name": "medicare-help", "brand": "Medicare Guide",
         "reason": "医保查询，独立建站"},
        {"term": "road tax", "name": "music", "brand": "X"},  # 已在站群 → 丢弃
    ])
    out = suggest_new_sites(
        [{"term": "medicare", "reason": "无匹配站点"},
         {"term": "road tax", "reason": "无匹配站点"}], proposals)
    assert out == [
        {"term": "medicare", "name": "medicare-help", "brand": "Medicare Guide",
         "domain": "medicare-help.toolara.dev", "reason": "医保查询，独立建站"},
        {"term": "road tax", "name": "road-tax", "brand": "Road Tax",
         "domain": "road-tax.toolara.dev", "reason": "无匹配站点"},
    ]


def test_render_report_shows_new_site_section_even_without_items():
    """全被排除时也出建议新站段（这正是用户要看的场景），且标注未建站。"""
    out = render_report([], [{"term": "medicare", "reason": "无匹配站点"}])
    assert "本轮无可排产词" in out
    assert "### 建议新站（待确认，本轮未建站）" in out
    assert "| medicare | Medicare | medicare.toolara.dev | 无匹配站点 |" in out
    assert "已一并列入待建页" in out


def test_plan_suggests_new_sites_into_planned(monkeypatch):
    """全是排除词时：建议新站也进待建页台账（站名=派生名），热词同步 wip。"""
    written = _capture(monkeypatch)
    seen = []
    monkeypatch.setattr(planning_mod.term_state_service, "after_plan",
                        lambda terms: seen.extend(terms))
    monkeypatch.setattr(planning_mod, "complete_json", _json({
        "excluded": [{"term": "medicare", "reason": "无匹配站点"}],
    }))

    out = planning_service.plan({})
    assert "### 建议新站（待确认，本轮未建站）" in out
    assert "| medicare | Medicare | medicare.toolara.dev |" in out
    assert "已写入待建页 1/1 条 planned 行" in out
    assert len(written) == 1
    assert written[0]["row"] == {"site": "medicare", "keyword": "medicare",
                                 "slug": "medicare", "status": "planned",
                                 "created_at": written[0]["row"]["created_at"]}
    assert seen == ["medicare"]


def test_intent_from_analysis_picks_first_section():
    """从 8 小节分析稿里抽「词义与意图」正文，压成单行。"""
    analysis = ("1) 词义与意图：判定今天是不是黑五，属日期类查询。\n\n"
                "2) 建议站型：倒计时页。\n3) 结论：做")
    assert intent_from_analysis(analysis) == "判定今天是不是黑五，属日期类查询。"


def test_intent_from_analysis_tolerates_heading_wording():
    """节标题措辞由 D1 维护，只认「词义 / 意图」词根，不逐字匹配。"""
    assert intent_from_analysis("1) 用户词义与意图：查退货政策\n2) 结论：做") == "查退货政策"
    assert intent_from_analysis("1) 建议站型：倒计时页\n2) 结论：做") == ""


def test_intent_from_analysis_escapes_table_cells():
    """换行/竖线会被压掉或换全角，避免切开 markdown 表格列。"""
    got = intent_from_analysis("1) 词义与意图：A 词 | B 词\n接着第二行\n2) 结论：做")
    assert "|" not in got and "｜" in got and "\n" not in got


def test_intent_from_analysis_empty_when_no_section():
    assert intent_from_analysis("") == ""
    assert intent_from_analysis(None) == ""


def test_term_intent_empty_without_analysis(fake_d1):
    term_id = fake_d1.seed_term("medicare")
    assert term_intent("medicare") == ""


def test_term_intent_reads_analysis_from_d1(fake_d1):
    term_id = fake_d1.seed_term("medicare")
    fake_d1.seed_analysis(
        term_id,
        text="1) 词义与意图\\nMedicare 是美国医疗保险资格查询。",
    )
    assert term_intent(" Medicare ") == "Medicare 是美国医疗保险资格查询。"

def test_term_intent_reads_analysis_from_kv(monkeypatch):
    """按 term_key 拼 key 读 ai:v3 分析稿并抽词义节。"""
    seen = []
    class KV:
        configured = True
        def get_text(self, key):
            seen.append(key)
            return "1) 词义与意图：医保资格查询\n2) 结论：做"
    monkeypatch.setattr(planning_mod, "get_kv", lambda: KV())
    assert term_intent(" Medicare ") == "医保资格查询"
    assert seen == ["ai:v3:medicare"]


def test_render_report_shows_intent_column(monkeypatch):
    """建议新站表格带「词义与意图」列，内容来自分析稿（读不到留空不影响列）。"""
    monkeypatch.setattr(planning_mod, "term_intent",
                        lambda term: "医保资格查询" if term == "medicare" else "")
    out = render_report([], [
        {"term": "medicare", "reason": "无匹配站点"},
        {"term": "is market open today", "reason": "无匹配站点"},
    ])
    assert "| 词 | 建议站名 | 品牌 | 建议域名 | 理由 | 词义与意图 |" in out
    assert "| medicare | medicare | Medicare | medicare.toolara.dev | 无匹配站点 | 医保资格查询 |" in out
    assert ("| is market open today | is-market-open-today | Is Market Open Today | "
            "is-market-open-today.toolara.dev | 无匹配站点 | — |") in out


def test_plan_ignores_model_written_report(monkeypatch):
    """模型自带 report 与 items 分叉时，报告以结构化数据为准，不采信自带正文。"""
    _capture(monkeypatch)
    monkeypatch.setattr(planning_mod, "complete_json", _json({
        "report": "## 本轮排产\n本轮不改站群",
        "items": [{"term": "blackout", "site": "clocksgoback"}],
        "excluded": [],
    }))

    out = planning_service.plan({})
    assert "本轮不改站群" not in out
    assert "| blackout | clocksgoback |" in out
