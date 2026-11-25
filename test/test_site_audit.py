# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用；详见 LICENSE.

"""站情通读：读原站 / 判重 / 风格继承 / 建页链集成。

只钉业务语义：同 slug 必拦、标题高度相似必拦、无关词可新增、增强模式显式放行、
写作用原站摘要、渲染继承首页 stylesheet。IO 用 monkeypatch 替换生产已有的公共 API。
"""

from __future__ import annotations

from pathlib import Path

from src.app import graph, services
from src.app.config import SITES
from src.app.enums import AuditDecision
from src.app.tools.adapters.publishing import renderer


INDEX = """<!DOCTYPE html>
<html><head>
<title>Black Friday 2026 Countdown</title>
<meta name="description" content="Live countdown and Black Friday dates">
<meta name="theme-color" content="#0b0b0d">
<link rel="stylesheet" href="/css/style.css">
<link rel="icon" href="/favicon.png">
</head><body>
<header class="site-header"><a class="logo" href="/">BF Countdown</a></header>
<main class="wrap"><h1>When Is Black Friday?</h1>
<p>Black Friday 2026 is on November 27. Deals start earlier every year.</p>
<h2>FAQ</h2></main>
<footer>Black Friday Countdown</footer>
</body></html>
"""

OLD_PAGE = """<html><head><title>Is It Black Friday Today? Check the Date</title>
<meta name="description" content="Find out if today is Black Friday"></head>
<body><header>BF Countdown</header><main><h1>Is It Black Friday Today</h1>
<p>Black Friday is the Friday after Thanksgiving, between November 23 and 29.</p>
</main><footer>BF Countdown</footer></body></html>
"""


def _site(tmp_path: Path, monkeypatch, pages: dict[str, str] | None = None):
    """把 blackfriday 的仓库指向 tmp_path，避免测试碰真仓库。"""
    public = tmp_path / "public"
    public.mkdir(parents=True, exist_ok=True)
    (public / "index.html").write_text(INDEX, encoding="utf-8")
    for name, html in (pages or {}).items():
        (public / name).write_text(html, encoding="utf-8")
    monkeypatch.setattr(services.site_audit_service, "ensure_repo", lambda _site: tmp_path)


def test_read_site_extracts_pages_and_style(tmp_path, monkeypatch):
    _site(tmp_path, monkeypatch, {"is-it-black-friday-today.html": OLD_PAGE})
    digest = services.site_audit_service.read_site("blackfriday")
    assert digest["ok"] is True and len(digest["pages"]) == 2
    home = next(p for p in digest["pages"] if not p["slug"])
    assert "Black Friday 2026 Countdown" in home["title"]
    assert "Thanksgiving" not in home["text"]      # 不串页
    assert digest["style"]["dark"] is True
    assert digest["style"]["has_countdown"] is True


def test_read_site_degrades_without_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(services.site_audit_service, "ensure_repo",
                        lambda _site: tmp_path / "missing")
    digest = services.site_audit_service.read_site("blackfriday")
    assert digest["ok"] is False and "不存在" in digest["reason"]
    assert services.site_audit_service.audit("any term", digest)["decision"] == AuditDecision.ADD


def test_audit_blocks_same_slug():
    digest = {"ok": True, "pages": [{"slug": "is-it-black-friday-today",
                                     "title": "Is It Black Friday Today?", "text": "",
                                     "headings": []}]}
    verdict = services.site_audit_service.audit("Is It Black Friday Today", digest)
    assert verdict["decision"] == AuditDecision.SKIP
    assert verdict["confidence"] == "slug" and verdict["score"] == 1.0
    assert "改走增强原页" in verdict["reason"]


def test_audit_blocks_similar_title_and_allows_unrelated():
    digest = {"ok": True, "pages": [{"slug": "", "title": "When Is Black Friday 2026",
                                     "text": "Black Friday is on November 27", "headings": []}]}
    same = services.site_audit_service.audit("when is black friday 2026", digest)
    assert same["decision"] == AuditDecision.SKIP and same["confidence"] == "title"
    other = services.site_audit_service.audit("sourdough hydration calculator", digest)
    assert other["decision"] == AuditDecision.ADD and other["score"] == 0.0


def test_audit_enhance_on_body_overlap():
    digest = {"ok": True, "pages": [{"slug": "dates", "title": "Holiday Dates",
                                     "text": "black friday dates countdown thanksgiving shopping",
                                     "headings": []}]}
    verdict = services.site_audit_service.audit("black friday", digest)
    assert verdict["decision"] == AuditDecision.ENHANCE
    assert verdict["confidence"] == "body"


def test_style_brief_carries_existing_pages_and_writing_rule():
    digest = {"ok": True, "pages": [{"slug": "", "title": "BF Countdown",
                                     "description": "Live countdown", "headings": ["FAQ"]}],
              "style": {"dark": True, "colors": ["#0b0b0d"], "has_countdown": True,
                        "has_faq": True, "title": "BF Countdown"}}
    brief = services.site_audit_service.style_brief(
        digest, {"decision": AuditDecision.ENHANCE.value, "reason": "正文已覆盖"})
    assert "深色" in brief and "首页有倒计时" in brief
    assert "只写原站尚未覆盖" in brief and "FAQ" in brief


def test_page_graph_runs_audit_between_plan_and_write(fake, fake_kv, monkeypatch):
    from test.test_page_build import _seed_todo, _stub_extract, _SITE_OK
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_kv, "free invoice generator")
    # 用 pure 桩替代 clone：仓库里没有这个词的页面 → 可新增
    monkeypatch.setattr(services.site_audit_service, "ensure_repo",
                        lambda _site: Path("/nonexistent-repo-for-test"))
    page = {str(n) for n in graph.get_page_graph().get_graph().nodes}
    assert "page_audit" in page
    out = graph.run_page_build("free invoice generator", confirm=False)
    assert out["auditDecision"] == "add" and out["mode"] == "auto"
    assert out["stage"] == "write" and fake.counts["render_files"] == 0


def test_duplicate_page_blocked_before_any_write(fake, fake_kv, monkeypatch, tmp_path):
    from test.test_page_build import _seed_todo, _stub_extract, _SITE_OK
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_kv, "is it black friday today")
    _site(tmp_path, monkeypatch, {"is-it-black-friday-today.html": OLD_PAGE})
    out = graph.run_page_build("is it black friday today", confirm=True)
    assert out["ok"] is False and out["stage"] == "audit"
    assert out["auditDecision"] == "skip" and out["auditScore"] == 1.0
    assert fake.counts["render_files"] == 0 and fake.counts["git_push"] == 0
    assert "不新增" in out["note"]


def test_enhance_mode_overrides_skip(fake, fake_kv, monkeypatch, tmp_path):
    from test.test_page_build import _seed_todo, _stub_extract, _SITE_OK
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_kv, "is it black friday today")
    _site(tmp_path, monkeypatch, {"is-it-black-friday-today.html": OLD_PAGE})
    out = graph.run_page_build("is it black friday today", confirm=True,
                               planned_site="invoices", mode="enhance")
    assert out["auditDecision"] == "enhance" and out["stage"] == "verify"
    assert out["mode"] == "enhance" and fake.counts["git_push"] == 1


def test_writing_payload_carries_site_brief(monkeypatch):
    captured = {}

    def fake_complete(system, user, **kwargs):
        captured["user"] = user
        return None, False, False

    monkeypatch.setattr(services.page_content_service, "complete_json", fake_complete)
    services.page_content_service.page_content_service.write(
        "is it black friday today", "blackfriday", "1) 词义与意图：判定今天是不是黑五",
        site_brief="原站已有：首页倒计时与日期表")
    assert "site_brief" in captured["user"]
    assert "原站已有：首页倒计时与日期表" in captured["user"]
    assert "[原站现状与写作约束]" in captured["user"]  # 并进 analysis 素材，提示词不认识字段也能读到


def test_render_inherits_site_stylesheet(tmp_path, monkeypatch):
    work = tmp_path / "repo"
    public = work / "public"
    public.mkdir(parents=True)
    (public / "index.html").write_text(INDEX, encoding="utf-8")
    skin = renderer._site_skin(work, SITES["blackfriday"])
    assert "/css/style.css" in skin["styles"]
    assert "theme-color" in skin["theme"] and "site-header" in skin["header"]
    out = renderer.render_page(SITES["blackfriday"],
                               {"slug": "test-page", "title": "Test", "description": "D",
                                "h1": "Test", "intro": "i", "sections": [], "faq": []}, skin)
    assert 'rel="stylesheet"' in out and "site-header" in out
    assert 'application/ld+json' in out and "--ink" in out
