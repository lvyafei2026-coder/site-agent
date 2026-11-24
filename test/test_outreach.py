# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""外链推广（find_backlinks）与复盘邮件（review_metrics）两条自动化支线。

全部经 monkeypatch 打桩 LLM / Dev.to / Resend，不发真请求、不发真邮件。
"""

from __future__ import annotations

import json

from src.app.config import SiteConfig
from src.app.services import outreach_service as outreach_mod
from src.app.services import metrics_service as metrics_mod

_GOOD_JSON = json.dumps({
    "title": "5 free invoice tools for freelancers",
    "tags": ["ai", "tools"],
    "target_url": "https://freshsite.toolara.dev/",
    "draft": "# Free invoice tools\n\nBody here.",
})


def _patch_llm(monkeypatch, raw: str, captured: dict | None = None):
    def fake_complete(system, user, warn_event=""):
        if captured is not None:
            captured["system"] = system
            captured["user"] = user
        return raw
    monkeypatch.setattr(outreach_mod, "complete_text", fake_complete)


def test_outreach_draft_and_publish(monkeypatch):
    """LLM 产出合法 JSON → 按解析结果调 devto.publish，回执带 source_url。"""
    captured: dict = {}
    _patch_llm(monkeypatch, _GOOD_JSON, captured)
    published: dict = {}
    monkeypatch.setattr(outreach_mod.devto, "publish",
                        lambda payload: published.update(payload)
                        or {"ok": True, "status": "published",
                            "detail": "dev.to 已发布", "source_url": "https://dev.to/x"})

    out = outreach_mod.outreach_service.run(
        {"list_devto_articles": {"ok": True, "result": [{"title": "t"}]}},
        {"target_site": "freshsite"}, {"promo_article": "v1"})

    assert out["ok"] is True and out["source_url"] == "https://dev.to/x"
    assert published["title"] == "5 free invoice tools for freelancers"
    assert published["target_url"] == "https://freshsite.toolara.dev/"
    # 提示词从 D1 加载（桩），payload 压缩进了 user 消息
    assert "promo_article" in captured["system"]
    assert "list_devto_articles" in captured["user"]


def test_outreach_target_url_falls_back_to_sites(monkeypatch):
    """LLM 没给链接时，按 extracted.target_site 查站群清单取 url。"""
    monkeypatch.setattr(outreach_mod, "SITES", {
        "freshsite": SiteConfig(name="freshsite", repo_name="fresh-site-tool",
                                subdomain="freshsite", brand="x"),
    })
    _patch_llm(monkeypatch, json.dumps({"title": "t", "draft": "d"}))
    published: dict = {}
    monkeypatch.setattr(outreach_mod.devto, "publish",
                        lambda payload: published.update(payload)
                        or {"ok": True, "status": "published", "detail": "", "source_url": ""})

    outreach_mod.outreach_service.run({}, {"target_site": "FreshSite"})

    assert published["target_url"] == "https://freshsite.toolara.dev"


def test_outreach_bad_json_never_publishes(monkeypatch):
    """LLM 没吐合法 JSON → draft_failed，绝不碰发布。"""
    _patch_llm(monkeypatch, "抱歉，我写了一篇很棒的文章……")
    called = []
    monkeypatch.setattr(outreach_mod.devto, "publish",
                        lambda payload: called.append(payload) or {})

    out = outreach_mod.outreach_service.run({}, {})

    assert out["ok"] is False and out["status"] == "draft_failed"
    assert called == []


def test_metrics_notify_sends_and_skips(monkeypatch):
    """邮件正文按行转义换 <br>；Resend 返回 False 时如实回传 sent=False。"""
    sent: dict = {}
    monkeypatch.setattr(metrics_mod.email, "send",
                        lambda subject, html, to=None: sent.update(subject=subject,
                                                                   html=html) or True)
    out = metrics_mod.metrics_service.notify("结论：A 站最好\n排名：A > B")
    assert out["sent"] is True and "流量复盘 · " in out["subject"]
    assert sent["html"] == "结论：A 站最好<br>排名：A &gt; B"

    monkeypatch.setattr(metrics_mod.email, "send", lambda subject, html, to=None: False)
    assert metrics_mod.metrics_service.notify("x")["sent"] is False


def test_graph_review_metrics_sends_email(graph, fake, monkeypatch):
    """复盘整轮：分析正文并入报告，邮件回执 metrics_notice 写进 state。"""
    monkeypatch.setattr(metrics_mod, "complete_text",
                        lambda system, user, warn_event="": "A 站表现最好")
    monkeypatch.setattr(metrics_mod.email, "send",
                        lambda subject, html, to=None: True)

    r = graph.invoke({"input_text": "复盘各站点击表现", "intent": "review_metrics",
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-1"}})

    assert r["metrics_analysis"] == "A 站表现最好"
    assert r["metrics_notice"]["sent"] is True
    assert "sent=True" in r["output"]


def test_graph_find_backlinks_drafts_and_publishes(graph, fake, monkeypatch):
    """外链整轮：取数后自动起稿 + 发布，回执 outreach_result 写进 state 与报告。"""
    _patch_llm(monkeypatch, _GOOD_JSON)
    monkeypatch.setattr(outreach_mod.devto, "publish",
                        lambda payload: {"ok": True, "status": "published",
                                         "detail": "dev.to 已发布",
                                         "source_url": "https://dev.to/x"})

    r = graph.invoke({"input_text": "为新站点找外链机会", "intent": "find_backlinks",
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-1"}})

    assert r["outreach_result"]["ok"] is True
    assert r["outreach_result"]["article"]["title"] == "5 free invoice tools for freelancers"
    assert "外链发布" in r["output"]


def test_graph_find_backlinks_draft_failure_does_not_block(graph, fake, monkeypatch):
    """起稿失败只如实记录（draft_failed），整轮照常 success 出报告。"""
    _patch_llm(monkeypatch, "不是 JSON 的一段话")

    r = graph.invoke({"input_text": "找外链机会", "intent": "find_backlinks",
                      "retry_count": 0, "messages": [], "tool_results": {}},
                     config={"configurable": {"thread_id": "t-1"}})

    assert r["outreach_result"]["status"] == "draft_failed"
    assert "status=success" in r["output"]
