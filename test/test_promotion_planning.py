# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用；详见 LICENSE.

"""推广排期：近 30 天新页 → 排期条目 → 落 agent_reports(kind='plan')。

测试全部走生产公共 API（tool_registry.bind 的 fake 门面 + write_promo_slot 真实现），
不给业务函数塞注入参数；纯计算（build_slots / render_report）单独验口径。
"""

from __future__ import annotations

import json

import src.app.services.promotion_planning_service as promo_mod
from src.app.services.promotion_planning_service import (
    build_slots,
    promotion_planning_service,
    render_report,
)

SITES_SAMPLE = {
    "music": type("S", (), {"url": "https://music.example.com"})(),
    "humanize": type("S", (), {"url": "https://humanize.example.com"})(),
}


def _pages(*pairs):
    """(site, slug, keyword) → get_page_inventory 的 result。"""
    return [{"site": s, "slug": g, "keyword": k, "status": "deployed"}
            for s, g, k in pairs]


def _results(pages, latest=None, events=None):
    return {
        "get_page_inventory": {"ok": True, "result": pages},
        "get_latest_plan": {"ok": True, "result": latest or []},
        "get_pending_events": {"ok": True, "result": events or []},
    }


def test_build_slots_schedules_new_pages_rotating_channels(monkeypatch):
    monkeypatch.setattr(promo_mod, "SITES", SITES_SAMPLE)
    slots, excluded = build_slots(_results(_pages(
        ("music", "moon", "when is full moon"),
        ("humanize", "essay", "essay humanizer"),
    )))
    assert len(slots) == 2 and not excluded
    assert {s["channel"] for s in slots} == {"devto", "reddit"}
    assert slots[0]["target_url"] == "https://music.example.com/moon"  # canonical，无 .html
    assert slots[0]["title"] == "when is full moon"
    # 日期从明天起顺延、严格递增
    assert slots[1]["date"] > slots[0]["date"]


def test_build_slots_excludes_already_promoted_and_unknown_site(monkeypatch):
    monkeypatch.setattr(promo_mod, "SITES", SITES_SAMPLE)
    latest = [{"site": "music", "slug": "moon", "date": "2026-01-01",
               "channel": "devto", "target_url": "https://music.example.com/moon"}]
    slots, excluded = build_slots(_results(_pages(
        ("music", "moon", "already done"),
        ("music", "new-one", "fresh"),
        ("ghost", "orphan", "no site"),
    ), latest=latest))
    assert [s["slug"] for s in slots] == ["new-one"]
    reasons = {e["page"]: e["reason"] for e in excluded}
    assert reasons["music/moon"] == "上版排期已推广"
    assert reasons["ghost/orphan"] == "站点不在群清单"


def test_build_slots_starts_after_latest_future_slot(monkeypatch):
    """上版排到更晚的日期时不挤压旧档，从最后一天之后接着排。"""
    monkeypatch.setattr(promo_mod, "SITES", SITES_SAMPLE)
    future = "2099-06-01"
    slots, _ = build_slots(_results(_pages(("music", "x", "x")), latest=[
        {"site": "humanize", "slug": "old", "date": future, "channel": "devto"},
    ]))
    assert slots[0]["date"] == "2099-06-02"


def test_render_report_is_same_data_as_slots():
    slots = [{"date": "2026-11-26", "channel": "devto", "site": "music",
              "slug": "moon", "target_url": "https://music.example.com/moon",
              "reason": "新页"}]
    text = render_report(slots, [{"page": "music/old", "reason": "已推广"}], 1, 3)
    assert "| 2026-11-26 | devto | music/moon |" in text
    assert "music/old — 已推广" in text
    assert "已写入台账 1/1 条" in text
    assert "3 条待消费事件" in text
    assert "没有可排期的已上线页" in render_report([], [], 0, 0)


def test_plan_writes_ledger_via_real_tool(fake, monkeypatch):
    """整轮落库走 write_promo_slot 真实现（fake 门面），报告与台账同源。"""
    monkeypatch.setattr(promo_mod, "SITES", SITES_SAMPLE)
    results = _results(_pages(("music", "moon", "when is full moon")),
                       events=[{"kind": "hotwords-ready"}])
    report = promotion_planning_service.plan(results)
    assert "已写入台账 1/1 条" in report
    plans = [r for r in fake.tables["agent_reports"] if r.get("kind") == "plan"]
    assert len(plans) == 1
    actions = json.loads(plans[0]["actions"])
    assert actions[0]["target_url"] == "https://music.example.com/moon"
    assert actions[0]["channel"] == "devto"
    assert "1 条待消费事件" in report


def test_plan_is_idempotent_against_last_plan(fake, monkeypatch):
    """重复点排期：上版已有该页 → 本轮不新增，也不会重复堆档期。"""
    monkeypatch.setattr(promo_mod, "SITES", SITES_SAMPLE)
    pages = _pages(("music", "moon", "when is full moon"))
    first = promotion_planning_service.plan(_results(pages))
    assert "已写入台账 1/1 条" in first

    latest = json.loads([r for r in fake.tables["agent_reports"]
                         if r.get("kind") == "plan"][0]["actions"])
    second = promotion_planning_service.plan(_results(pages, latest=latest))
    assert "没有可排期" in second
    plans = [r for r in fake.tables["agent_reports"] if r.get("kind") == "plan"]
    assert len(json.loads(plans[-1]["actions"])) == 1  # 没有追加重复项


def test_plan_survives_bad_tool_results(fake, monkeypatch):
    """工具失败 / 空结果不崩，报告如实说没有可排期页。"""
    monkeypatch.setattr(promo_mod, "SITES", SITES_SAMPLE)
    bad = {"get_page_inventory": {"ok": False, "error": "boom"},
           "get_latest_plan": {"ok": False, "error": "boom"}}
    assert "没有可排期" in promotion_planning_service.plan(bad)
