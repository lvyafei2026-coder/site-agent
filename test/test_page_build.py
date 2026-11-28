# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""建页线（第三条线）：解析 / 归类 / 取词 / 子图端到端 / 入站契约。

    POST /page  →  建页子图（plan → render → push → verify）

只钉业务语义与架构，不为胶水写测试；推送落台账（agent_pages）在 test_sql.py
用真 SQL 覆盖，这里 FakeRuntime 的 git_push 是纯桩。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.app import graph
from src.app.api.routes import api
from src.app.config import SITES
from src.app.graph import page_graph
from src.app.graph.state import initial_page_build_state
from src.app.observability.trace import run_traced
from src.app.services import page_build_service
from src.app.services import page_content_service as content_mod
from src.app.services.extraction_service import extraction_service
from src.app.tools.adapters.publishing import verification

_BODY = (
    "Free invoice generator overview paragraph that is definitely longer "
    "than twenty characters so the intro picks it up.\n"
    "\n"
    "1) What it does\n"
    "A paragraph explaining what the tool does, long enough to pass the\n"
    "paragraph length filter without any trouble at all.\n"
    "\n"
    "2) How to use it\n"
    "Another paragraph with plenty of length so it counts as a real\n"
    "content paragraph in the parsed page structure.\n"
)

_SITE_OK = {"target_site": "invoices"}


def _stub_extract(monkeypatch, extracted):
    monkeypatch.setattr(extraction_service, "extract",
                        lambda text, intent, versions=None: dict(extracted))


def _seed_todo(fake_d1, term, with_body=True, status="todo"):
    """在 D1 词库、人工状态与分析表中准备建页数据。"""
    term_id = fake_d1.seed_term(term)
    fake_d1.seed_status(term_id, status=status,
                        is_favorite=1 if status in ("watching", "wip", "live", "parked") else 0)
    if with_body:
        fake_d1.seed_analysis(term_id, text=_BODY, verdict="DO")
    return term_id


# ---- 图结构与机制同构 ----

def test_page_graph_is_a_third_distinct_line():
    """建页子图并列于另外两条线：节点集合独立、单例互不混用。"""
    page = {str(n) for n in graph.get_page_graph().get_graph().nodes}
    assert {"page_plan", "page_write", "page_render", "page_push", "page_verify"} <= page
    assert "classify_intent" not in page and "fetch_one" not in page
    assert graph.get_page_graph() is not graph.get_normal_graph()
    assert graph.get_page_graph() is not graph.get_hotword_graph()
    assert graph.get_page_graph is page_graph.get_page_graph


def test_page_line_shares_one_mechanism():
    """机制同构：build/get/run 三件套 + state 工厂 + 共用 run_traced 驱动器。"""
    assert page_graph.run_traced is run_traced
    assert callable(graph.build_page_graph) and callable(graph.run_page_build)
    s = initial_page_build_state("free invoice generator", confirm=True)
    assert s["term"] == "free invoice generator" and s["confirm"] is True
    assert s["planned_site"] == "" and s["status_filter"] == "todo" and s["stage"] == "plan"


def test_http_page_entry(fake, fake_d1, monkeypatch):
    """POST /page：dry-run 请求字段与 run_page_build 一一对应，返回终态汇总。"""
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_d1, "free invoice generator")
    client = TestClient(api)
    resp = client.post("/page", json={"term": "free invoice generator"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["dryRun"] is True
    assert body["stage"] == "write" and body["site"] == "invoices"
    assert "sha" not in body and "url" not in body
    # 从台账点单：词 + 排产站一起传，站点不再由 LLM 重判
    pinned = client.post("/page", json={"term": "free invoice generator",
                                        "planned_site": "clocksgoback"}).json()
    assert pinned["site"] == "clocksgoback" and pinned["classifyBy"] == "plan"
    assert client.post("/page", json={}).status_code == 200   # 全缺省也是合法 dry-run


# ---- 解析（parse_page）----

def test_parse_page_numbered_sections():
    """D1 分析正文按「数字) 标题」切节：节标题、段落、intro/description 都到位。"""
    page = page_build_service.parse_page("free invoice generator", _BODY)
    assert page["slug"] == "free-invoice-generator"
    assert page["title"] == "Free Invoice Generator"
    headings = [s["heading"] for s in page["sections"]]
    assert headings == ["What it does", "How to use it"]
    assert all(len(s["paragraphs"]) >= 1 for s in page["sections"])
    assert page["intro"].startswith("Free invoice generator overview")
    assert page["faq"] == []


def test_parse_page_never_fails():
    """没有编号节 → 整篇兜底单节；空文本 → 占位，任何输入都不抛错。"""
    loose = page_build_service.parse_page("cold brew ratio",
                                          "just one paragraph, long enough to survive the filter")
    assert len(loose["sections"]) == 1 and loose["sections"][0]["paragraphs"]
    empty = page_build_service.parse_page("some term", "")
    assert empty["slug"] == "some-term" and empty["sections"] == []


def test_slugify_normalizes():
    assert page_build_service.slugify("  When   Is BLACK Friday?! ") == "when-is-black-friday"
    assert page_build_service.slugify("!!!") == "page"


# ---- 归类（classify_site）----

def test_classify_site_llm_first_rule_fallback(monkeypatch):
    """LLM（extraction target_site）优先；LLM 给非法值退回规则；都归不进 → none。"""
    _stub_extract(monkeypatch, _SITE_OK)
    out = page_build_service.classify_site("free invoice generator", _BODY)
    assert out == {"site": "invoices", "by": "llm"}

    _stub_extract(monkeypatch, {"target_site": "nonexistent-site"})
    out = page_build_service.classify_site("black friday deals canada", _BODY)
    assert out["by"] == "rule" and out["site"] == "blackfriday"

    _stub_extract(monkeypatch, {"target_site": ""})
    out = page_build_service.classify_site("totally unrelated words here", "")
    assert out == {"site": "", "by": "rule"}     # 规则也没命中 → 空站点


def test_classify_site_rule_matches_semantic_aliases(monkeypatch):
    """语义别名整串命中：beaver-moon 接住 november full moon，但单词 moon 不误伤别的站。"""
    _stub_extract(monkeypatch, {"target_site": ""})
    # LLM 没给 → 走规则；别名 "november full moon" 整串命中 beaver-moon
    out = page_build_service.classify_site("november full moon", _BODY)
    assert out == {"site": "beaver-moon", "by": "rule"}

    # 别名作为 LLM 清单的一部分送出去（模型也能看到语义，不只站名面值）
    seen = {}
    def capture(text, intent, versions=None):
        seen["text"] = text
        return {"target_site": "beaver-moon"}
    monkeypatch.setattr(extraction_service, "extract", capture)
    page_build_service.classify_site("november full moon", _BODY)
    assert "别名" in seen["text"] and "november full moon" in seen["text"]


# ---- 取词（pick_word）----

def test_pick_word_explicit_and_auto(fake, fake_d1, monkeypatch):
    """显式给词只查该词；自动取按 saved 表顺序找第一条带正文的 todo 词。"""
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_d1, "free invoice generator")
    assert page_build_service.pick_word("free invoice generator")["text"].startswith("Free")

    _seed_todo(fake_d1, "cold brew ratio", with_body=False)
    assert page_build_service.pick_word("cold brew ratio") is None   # 无正文不建页
    assert page_build_service.pick_word()["term"] == "free invoice generator"

    live_id = fake_d1.seed_term("live page speed")
    fake_d1.seed_status(live_id, status="live", is_favorite=1)
    fake_d1.seed_analysis(live_id, text="x" * 40)
    assert page_build_service.todo_pool_size() == 2


def test_pick_word_skips_bodyless_todo(fake, fake_d1):
    """排在前面的 todo 词没正文 → 跳过它取下一条，绝不建无分析词。"""
    _seed_todo(fake_d1, "no body word", with_body=False)
    _seed_todo(fake_d1, "body ready word")
    assert page_build_service.pick_word()["term"] == "body ready word"


def test_pick_word_prefers_planned_rows(fake, fake_d1, monkeypatch):
    """排产入选的 planned 行优先：连目标站点一起带出来，不被池顺序和重判抢走。"""
    _seed_todo(fake_d1, "pool order word")
    _seed_todo(fake_d1, "planned word", with_body=True)
    monkeypatch.setattr(page_build_service, "_planned_rows", lambda limit=5: [
        {"site": "invoices", "keyword": "planned word", "slug": "planned-word"},
    ])
    picked = page_build_service.pick_word()
    assert picked["term"] == "planned word"
    assert picked["site"] == "invoices"


def test_pick_word_falls_back_when_plan_has_no_body(fake, fake_d1, monkeypatch):
    """计划行对应的词没正文（不该发生）→ 跳过它回退池顺序，不建空页面。"""
    _seed_todo(fake_d1, "body ready word")
    monkeypatch.setattr(page_build_service, "_planned_rows", lambda limit=5: [
        {"site": "invoices", "keyword": "planned without body", "slug": "planned-without-body"},
    ])
    assert page_build_service.pick_word()["term"] == "body ready word"


# ---- 子图端到端 ----

def test_run_page_build_dry_run_touches_nothing(fake, fake_d1, monkeypatch):
    """confirm=False：plan 出结构与站点就收尾，render/push/verify 一个都不跑。"""
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_d1, "free invoice generator")
    out = graph.run_page_build("", confirm=False)
    assert out["ok"] is True and out["dryRun"] is True and out["stage"] == "write"
    assert out["site"] == "invoices" and out["classifyBy"] == "llm"
    assert out["page"]["slug"] == "free-invoice-generator"
    assert fake.counts["render_files"] == 0 and fake.counts["git_push"] == 0
    assert "note" in out and "dry-run" in out["note"]


def test_run_page_build_confirm_full_chain(fake, fake_d1, monkeypatch):
    """confirm=True：render → push → verify 全链跑完，台账由 push 工具层负责。"""
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_d1, "free invoice generator")
    out = graph.run_page_build("free invoice generator", confirm=True)
    assert out["ok"] is True and out["stage"] == "verify"
    assert out["sha"] == "deadbeef" and out["liveOk"] is True
    assert out["url"].endswith("free-invoice-generator?v=1")
    assert fake.counts["render_files"] == 1 and fake.counts["git_push"] == 1


def test_run_page_build_stops_when_nothing_to_classify(fake, fake_d1, monkeypatch):
    """归不进任何已有站 → plan 短路，写工具一次都不碰，错误说明去向。"""
    _stub_extract(monkeypatch, {"target_site": ""})
    _seed_todo(fake_d1, "totally unrelated words here")
    out = graph.run_page_build("", confirm=True)
    assert out["stage"] == "plan" and "建新站" in out["error"]
    assert fake.counts["render_files"] == 0 and fake.counts["git_push"] == 0


def test_run_page_build_empty_pool(fake, fake_d1, monkeypatch):
    """池里没有可建词：正常收尾、带提示，不算异常。"""
    _stub_extract(monkeypatch, _SITE_OK)
    out = graph.run_page_build("", confirm=True)
    assert out["ok"] is True and out["stage"] == "plan"
    assert "待开发池" in out["error"] and out["todoPool"] == 0


def test_run_page_build_uses_planned_site_over_llm(fake, fake_d1, monkeypatch):
    """从台账点单时带上的排产站优先：LLM 想判 invoices，也不许改投。"""
    _stub_extract(monkeypatch, _SITE_OK)          # LLM 会说 invoices
    _seed_todo(fake_d1, "free invoice generator")
    out = graph.run_page_build("free invoice generator", confirm=False,
                               planned_site="clocksgoback")
    assert out["site"] == "clocksgoback" and out["classifyBy"] == "plan"


def test_run_page_build_ignores_unknown_planned_site(fake, fake_d1, monkeypatch):
    """台账里留着已删站的旧行时，不能拿去渲染：当没给，回退现场归类。"""
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_d1, "free invoice generator")
    out = graph.run_page_build("free invoice generator", confirm=False,
                               planned_site="gone-site")
    assert out["site"] == "invoices" and out["classifyBy"] == "llm"


# ---- 验活（verify_live）：地址口径 + 构建/生效延迟 ----

def _resp(code, text=""):
    return type("R", (), {"status_code": code, "text": text})()


def test_verify_live_prefers_canonical_url(monkeypatch):
    """CF Workers 会把 /x.html 307 到 /x：先打规范地址，命中就不再试扩展名。"""
    seen = []

    def fake_get(url, timeout=20):
        seen.append(url)
        return _resp(200, "Is It Black Friday Today")

    monkeypatch.setattr(verification.requests, "get", fake_get)
    ok, url = verification.verify_live(SITES["blackfriday"], "is-it-black-friday-today",
                                       "Is It Black Friday Today", attempts=1, delay_sec=0)
    assert ok is True and ".html" not in url
    assert url.startswith("https://blackfriday.toolara.dev/is-it-black-friday-today?v=")


def test_verify_live_falls_back_to_html_and_retries(monkeypatch):
    """只认扩展名的宿主：规范地址 404 后退 .html；构建未完成时按轮重试。"""
    seen = []

    def fake_get(url, timeout=20):
        seen.append(url)
        if ".html" in url:
            return _resp(200, "Is It Black Friday Today")
        return _resp(404)                       # 规范地址不存在

    monkeypatch.setattr(verification.requests, "get", fake_get)
    ok, url = verification.verify_live(SITES["blackfriday"], "is-it-black-friday-today",
                                       "Is It Black Friday Today", attempts=1, delay_sec=0)
    assert ok is True and ".html" in url
    assert ".html" not in seen[0]               # 先试规范地址，再退扩展名


def test_verify_live_reports_404_after_retries(monkeypatch):
    """全 404：重试到上限后如实失败，detail 带状态码（别把「还在构建」说成成功）。"""
    calls = {"n": 0}

    def fake_get(url, timeout=20):
        calls["n"] += 1
        return _resp(404)

    monkeypatch.setattr(verification.requests, "get", fake_get)
    ok, detail = verification.verify_live(SITES["blackfriday"], "is-it-black-friday-today",
                                          "", attempts=2, delay_sec=0)
    assert ok is False and "404" in detail
    assert calls["n"] == 4        # 每轮两个地址 × 两轮


# ---- 正文写作（page_content）：分析稿 → 访客正文，渲染前拦分析元话语 ----

def _llm_page():
    return {
        "title": "Is It Black Friday Today? Check the Date",
        "description": "A quick answer to whether today is Black Friday and how the date is set.",
        "h1": "Is It Black Friday Today?",
        "intro": "Black Friday falls after Thanksgiving in the United States.",
        "sections": [{"heading": "How the date works",
                      "paragraphs": [" retailers open on the Friday after Thanksgiving.",
                                      " The date moves with Thanksgiving each year."]}],
        "faq": [{"q": "Is Black Friday a holiday?", "a": "It is not a federal holiday."}],
    }


def test_page_write_replaces_draft_with_written_copy(fake, fake_d1, monkeypatch):
    """LLM 给正文时：只覆盖文案字段，slug 仍由词决定，汇总标 contentBy=llm。"""
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_d1, "free invoice generator")
    monkeypatch.setattr(content_mod, "complete_json",
                        lambda *_args, **_kwargs: (_llm_page(), False, False))
    out = graph.run_page_build("free invoice generator", confirm=False)
    assert out["stage"] == "write" and out["contentBy"] == "llm"
    assert out["page"]["title"] == _llm_page()["title"]
    assert out["page"]["slug"] == "free-invoice-generator"
    assert out["page"]["faq"] == _llm_page()["faq"]
    assert fake.counts["render_files"] == 0
    assert "正文已按访客口吻写好" in out["note"]


def test_page_write_blocks_analysis_prose_before_render(fake, fake_d1, monkeypatch):
    """写作没成功、解析稿仍是内部决策稿 → 在渲染前拦下，一个写工具都不碰。"""
    _stub_extract(monkeypatch, _SITE_OK)
    black_friday_id = fake_d1.seed_term("black friday today")
    fake_d1.seed_status(black_friday_id, status="todo")
    fake_d1.seed_analysis(black_friday_id, text=(
        "1) 词义与意图：判断今天是不是黑五。\n\n"
        "2) 建议站型：倒计时 + 日期对照页，接住日期类长尾。"))
    monkeypatch.setattr(content_mod, "complete_json",
                        lambda *_args, **_kwargs: (None, False, False))
    out = graph.run_page_build("black friday today", confirm=True)
    assert out["ok"] is False and out["stage"] == "write"
    assert "渲染前拦下" in out["error"]
    # 失败信息要能区分「改写没产出」与「改写稿带内部用语」，先报出改写的失败原因
    assert "回退到解析稿" in out["error"] and "LLM 没返回可解析" in out["error"]
    assert fake.counts["render_files"] == 0 and fake.counts["git_push"] == 0


def test_page_write_reports_prompt_load_failure(fake, fake_d1, monkeypatch):
    """page_content 提示词读不到时，失败信息直接点名，不用去日志里猜是哪一步。"""
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_d1, "free invoice generator", with_body=False)
    free_invoice_id = fake_d1.query("SELECT term_id FROM radar_terms WHERE term_key = ?", ["free invoice generator"])[0]["term_id"]
    fake_d1.seed_analysis(free_invoice_id, text=(
        "1) 词义与意图：给自由职业者开票。\n\n"
        "A paragraph explaining what the tool does, long enough to pass the\n"
        "paragraph length filter without any trouble at all.\n\n"
        "2) 结论：做\n\n"
        "Another paragraph with plenty of length so it counts as a real\n"
        "content paragraph in the parsed page structure."))

    class BrokenPrompts:
        def load_prompt(self, *_args, **_kwargs):
            raise RuntimeError("提示词 page_content/v1 无法从 D1 读取")

    monkeypatch.setattr(content_mod, "prompt_service", BrokenPrompts())
    out = graph.run_page_build("free invoice generator", confirm=True)
    assert out["ok"] is False and out["stage"] == "write", out
    assert "page_content 提示词读不到" in out["error"]
    assert "回退到解析稿" in out["error"]
    assert fake.counts["render_files"] == 0


def test_page_write_blocks_written_copy_that_copied_analysis_words(fake, fake_d1, monkeypatch):
    """LLM 写出来了、但把分析稿小标题抄进正文：同样拦下，消息指明是改写稿。"""
    _stub_extract(monkeypatch, _SITE_OK)
    _seed_todo(fake_d1, "free invoice generator")
    page = _llm_page()
    page["sections"][0]["heading"] = "词义与意图"
    monkeypatch.setattr(content_mod, "complete_json",
                        lambda *_args, **_kwargs: (page, False, False))
    out = graph.run_page_build("free invoice generator", confirm=True)
    assert out["ok"] is False and out["stage"] == "write" and out["contentBy"] == "llm"
    assert "LLM 改写稿" in out["error"] and "词义与意图" in out["error"]
    assert fake.counts["render_files"] == 0


def test_analysis_leak_checks_every_rendered_field():
    """校验覆盖 title/intro/sections/faq，不能只查标题就放过正文里的分析话。"""
    page = dict(_llm_page())
    assert content_mod.analysis_leak(page) == ""
    page["sections"][0]["paragraphs"].append("6) 变现：affiliate 为主，购物节流量带购买意图。")
    assert content_mod.analysis_leak(page) == "变现："


def test_normalize_page_drops_broken_sections_and_envelope():
    """嵌套 page 外壳可拆；空节/空 FAQ 丢掉，结构仍能被 renderer 消费。"""
    raw = {"page": {"title": "T", "sections": [
        {"heading": "", "paragraphs": ["正文"]},
        {"heading": "Works", "paragraphs": [" something valid here "]},
    ], "faq": [{"q": "Q?", "a": ""}, {"question": "Q?", "answer": "A."}]}}
    out = content_mod.normalize_page(raw, "term")
    assert out is not None and "slug" not in out
    assert [s["heading"] for s in out["sections"]] == ["Works"]
    assert out["faq"] == [{"q": "Q?", "a": "A."}]
    assert content_mod.normalize_page({"sections": []}, "term") is None


def test_page_push_auto_registers_proxy_sitemap(fake, fake_d1, monkeypatch):
    """推送成功后自动把规范 URL 登记进代理 sitemap；失败只记录、不影响上线。"""
    from src.app.nodes import page_nodes
    from src.app.services import sitemap_service

    seen = []
    monkeypatch.setattr(sitemap_service, "register_page",
                        lambda site, slug: (seen.append((site, slug)) or
                                            {"ok": True, "changed": True, "path": "/" + slug}))
    monkeypatch.setattr(fake, "git_push", lambda site_name, page, work: {
        "ok": True, "skipped": False, "sha": "abc123", "slug": page["slug"]})

    state = initial_page_build_state("next full moon", confirm=True)
    state.update({"site": "clocksgoback", "picked": {"term": "next full moon"},
                  "page": {"slug": "next-full-moon"}, "work": "/tmp/fake-repo"})
    out = page_nodes.page_push(state)
    assert out["sitemap"]["ok"] is True and out["sitemap"]["changed"] is True
    assert seen == [("clocksgoback", "next-full-moon")]


def test_new_site_skin_renders_card_layout_and_lists_page_on_home(monkeypatch, tmp_path):
    """v2 骨架：内页卡片化 + 首页自动列文章；占位提示让位给真实卡片。"""
    from src.app.config import SiteConfig
    from src.app.tools.adapters.hosting import site_scaffold
    from src.app.tools.adapters.publishing import renderer

    site = SiteConfig(name="beaver-moon", repo_name="beaver-moon",
                      subdomain="beaver-moon", brand="Beaver Moon")
    work = tmp_path / "beaver-moon"
    public = work / "public"
    public.mkdir(parents=True)
    (public / "index.html").write_text(
        site_scaffold._index_html(site, "November moon guide"), encoding="utf-8")
    page = {
        "slug": "beaver-moon", "title": "Beaver Moon Explained",
        "description": "Why November's full moon has that name.",
        "h1": "Beaver Moon Explained", "intro": "The short answer.",
        "sections": [
            {"heading": "What it is", "paragraphs": ["A full moon in November."]},
            {"heading": "Why the name", "paragraphs": ["Beavers stocked dams."]},
        ],
        "faq": [{"q": "Is it a supermoon?", "a": "Not always."}],
    }
    monkeypatch.setenv("GH_ORG", "acme")
    monkeypatch.setattr(renderer, "ensure_repo", lambda _s: work)
    monkeypatch.setattr(renderer, "git", lambda *_a, **_k: None)
    monkeypatch.setattr(renderer, "has_staged_changes", lambda _w: True)

    out = renderer.render_files(site, page)
    assert out["ok"] is True and out["slug"] == "beaver-moon"
    html = (public / "beaver-moon.html").read_text(encoding="utf-8")
    assert "/assets/site.css" in html and "section-card" in html
    assert "faq-item" in html and "On this page" in html and 'href="/"' in html
    home = (public / "index.html").read_text(encoding="utf-8")
    assert 'href="/beaver-moon"' in home and "Beaver Moon Explained" in home
    assert "Coming soon" not in home
    assert site_scaffold.PAGES_START in home and site_scaffold.PAGES_END in home


def test_old_site_skin_keeps_original_renderer(monkeypatch, tmp_path):
    """没引 site.css 的老站：不加卡片布局，首页没有标记就不动。"""
    from src.app.config import SiteConfig
    from src.app.tools.adapters.publishing import renderer

    site = SiteConfig(name="old", repo_name="old", subdomain="old", brand="Old")
    work = tmp_path / "old"
    public = work / "public"
    public.mkdir(parents=True)
    (public / "index.html").write_text('<html><body><header>Old</header></body></html>',
                                       encoding="utf-8")
    page = {"slug": "old-page", "title": "Old page", "description": "d", "intro": "i",
            "sections": [], "faq": []}
    monkeypatch.setenv("GH_ORG", "acme")
    monkeypatch.setattr(renderer, "ensure_repo", lambda _s: work)
    monkeypatch.setattr(renderer, "git", lambda *_a, **_k: None)
    monkeypatch.setattr(renderer, "has_staged_changes", lambda _w: False)
    renderer.render_files(site, page)
    html = (public / "old-page.html").read_text(encoding="utf-8")
    assert "agent-faq" in html and "section-card" not in html
    assert (public / "index.html").read_text(encoding="utf-8").startswith("<html>")
