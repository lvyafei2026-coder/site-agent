# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""排产服务的落库契约：入选词必须变成 planned 台账行，不然建页线拿不到活。"""

import json

import src.app.services.planning_service as planning_mod
from src.app.services.planning_service import planning_service, valid_items

REPORT = "## 本轮排产\n\n### 入选\n| 优先级 | 词 | 站 |\n|---|---|---|\n| 高 | blackout | clocksgoback |"


def _rows(written):
    return [item["row"] for item in written]


def test_plan_saves_planned_rows_and_returns_report(monkeypatch):
    """结构化输出 → 入选词按顺序写 planned 行，返回给人工看的是 report 而非原始 JSON。"""
    written = []
    monkeypatch.setattr(planning_mod, "complete_text", lambda *a, **k: json.dumps({
        "items": [{"term": "blackout", "site": "clocksgoback", "priority": "高",
                   "reason": "停电查询"},
                  {"term": "full moon", "site": "invoices", "priority": "中",
                   "reason": "月相"}],
        "excluded": [{"term": "tax form", "reason": "无匹配站点"}],
        "report": REPORT,
    }, ensure_ascii=False))
    monkeypatch.setattr(planning_mod._ledger, "ledger_upsert",
                        lambda table, row: written.append({"table": table, "row": row}))

    out = planning_service.plan({"get_hotwords": {"ok": True, "result": {}}})

    assert out == REPORT
    assert [r["keyword"] for r in _rows(written)] == ["blackout", "full moon"]
    assert all(r["status"] == "planned" for r in _rows(written))
    assert _rows(written)[0]["site"] == "clocksgoback"


def test_plan_drops_unknown_site_and_bodyless_json(monkeypatch):
    """site 不在站群清单里的条目丢掉（LLM 会编站名）；一条都不合法时不写库。"""
    written = []
    monkeypatch.setattr(planning_mod, "complete_text", lambda *a, **k: json.dumps({
        "items": [{"term": "blackout", "site": "site-that-does-not-exist"}],
        "report": REPORT,
    }))
    monkeypatch.setattr(planning_mod._ledger, "ledger_upsert",
                        lambda table, row: written.append({"table": table, "row": row}))

    assert planning_service.plan({}) == REPORT
    assert written == []


def test_plan_returns_free_text_without_saving(monkeypatch):
    """模型没按 JSON 输出（换提示词期间可能出现）：报告照常展示，但这一轮无结构化入选词。"""
    written = []
    monkeypatch.setattr(planning_mod, "complete_text", lambda *a, **k: "## 本轮排产\n手写报告")
    monkeypatch.setattr(planning_mod._ledger, "ledger_upsert",
                        lambda table, row: written.append({"table": table, "row": row}))

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
