# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""排产服务的落库契约：入选词必须变成 planned 台账行，不然建页线拿不到活。"""

import src.app.services.planning_service as planning_mod
from src.app.services.planning_service import (
    planning_service, render_report, suggest_new_sites,
    valid_excluded, valid_items, valid_new_sites,
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
    assert "已写入台账 2/2 条" in out
    assert [r["keyword"] for r in _rows(written)] == ["blackout", "full moon"]
    assert all(r["status"] == "planned" for r in _rows(written))
    assert _rows(written)[0]["site"] == "clocksgoback"


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
    assert "本轮不建站、不写台账" in out


def test_plan_suggests_new_sites_without_side_effects(monkeypatch):
    """全是排除词时：报告出建站清单，但不写台账、不改热词状态。"""
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
    assert written == []
    assert seen == []


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
