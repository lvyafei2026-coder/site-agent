# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""页面渲染与 sitemap：只动本地文件，可逆。"""

from __future__ import annotations

import html as html_mod
import json
import re
from pathlib import Path
from typing import Any

from src.app.config import TEMPLATE_DIR, SiteConfig

from ..hosting.site_scaffold import PAGES_END, PAGES_START
from .github_repo import ensure_repo, git, has_staged_changes

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")

# ★规范 URL 唯一口径：不带 .html。CF Workers Assets 会把 /x.html 307 到 /x，
# canonical / JSON-LD / 代理 sitemap 必须用同一个地址，否则 Google 收到互相打架的信号。
def canonical_url(site: SiteConfig, slug: str) -> str:
    return f"https://{site.host}/{slug}"

# 新页内容区的最小样式：优先吃原站 CSS 变量（--ink / --accent / --line），
# 没有的站点走 fallback。这样同一站的新页与首页深浅、配色、字重自然一致。
_SKIN_CSS = """
<style>
.agent-page{max-width:860px;margin:0 auto;padding:36px 20px 64px}
.agent-page h1,.agent-page h2,.agent-page h3{color:var(--ink,var(--fg,#111827))}
.agent-page h1{font-size:clamp(1.7rem,3vw,2.2rem);line-height:1.25;margin:0 0 14px}
.agent-page h2{font-size:1.3rem;margin:34px 0 10px}
.agent-page h3{font-size:1.05rem;margin:18px 0 4px}
.agent-page p,.agent-page li{line-height:1.75;color:var(--ink-soft,var(--muted,#4b5563))}
.agent-page a{color:var(--accent,#185fa5)}
.agent-faq{border-top:1px solid var(--line,#e5e7eb);margin-top:40px;padding-top:8px}
</style>
"""

# v2 骨架站（首页引了 /assets/site.css）的内页增强：只补内页专属布局，
# 配色/字体/深浅模式全部继承全局 site.css，避免两处样式互相打架。
_SKIN_CSS_V2 = """
<style>
.agent-page{max-width:860px;margin:0 auto;padding:0 20px 72px}
.agent-page .crumbs{display:flex;gap:9px;align-items:center;padding:22px 0 0;color:var(--muted);font-size:.84rem}
.agent-page .crumbs a{color:var(--muted)}
.agent-page .lede{padding:30px 0 26px;border-bottom:1px solid var(--line);margin-bottom:30px}
.agent-page .tag{display:inline-block;background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:4px 12px;font-size:.72rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}
.agent-page .lede h1{font-size:clamp(1.9rem,4vw,2.6rem);line-height:1.16;letter-spacing:-.03em;margin:14px 0 12px;color:var(--ink)}
.agent-page .lede .intro{font-size:1.08rem;color:var(--ink-soft);max-width:720px;margin:0}
.agent-page .toc{display:flex;flex-wrap:wrap;gap:10px 20px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 20px;margin:0 0 30px;font-size:.9rem}
.agent-page .toc-title{font-weight:750;color:var(--ink)}
.agent-page .section-card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius,18px);padding:24px 26px;margin:0 0 20px;box-shadow:var(--shadow,none)}
.agent-page .section-card h2{font-size:1.32rem;letter-spacing:-.02em;margin:0 0 12px;color:var(--ink)}
.agent-page .section-card p{margin:0 0 12px;color:var(--ink-soft)}
.agent-page .section-card p:last-child{margin-bottom:0}
.agent-page .faq-section{margin-top:44px}
.agent-page .faq-head{font-size:1.5rem;letter-spacing:-.02em;margin:0 0 20px}
.agent-page .faq-item{border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:12px;background:var(--card);padding:18px 22px;margin-bottom:14px}
.agent-page .faq-item h3{font-size:1.02rem;margin:0 0 6px;color:var(--ink)}
.agent-page .faq-item p{margin:0;color:var(--ink-soft)}
.agent-page .back{margin-top:40px;font-size:.92rem}
@media(max-width:640px){.agent-page .section-card{padding:20px 18px}.agent-page .lede{padding-top:24px}}
</style>
"""


def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _header_brand(header_html: str, fallback: str) -> str:
    """页头只继承品牌标识，不搬首页的业务导航与动态状态。

    黑五页事故：原样拷首页 header 会把 `<nav><a href="#dates">…</a></nav>` 和
    `<span id="headerStatus">Loading…</span>` 一起带过来——新页没有 #dates 章节、
    也没引 countdown.js，于是导航点了没反应、状态永远停在 Loading…。
    """
    if not header_html:
        return ""
    logo = re.search(
        r'<a\b[^>]*class=["\'][^"\']*\blogo\b[^"\']*["\'][^>]*>[\s\S]*?</a>',
        header_html, flags=re.I)
    brand = logo.group(0) if logo else ""
    if not brand:
        # 没有 logo 标记的站：取页头纯文本当品牌，不带任何锚点或 id 状态位
        text = " ".join(re.sub(r"<[^>]+>", " ", header_html).split())
        brand = f'<a href="/">{esc(text or fallback)}</a>'
    return f'<header class="site-header"><div class="header-inner">{brand}</div></header>'


def _site_skin(work: Path, site: SiteConfig) -> dict[str, str]:
    """从站点首页抽可继承的骨架：样式表链接、主题色、品牌页头、页脚。

    只继承「视觉骨架」（配色/字体/配色变量/品牌标识/页脚），不继承首页的业务模块：
    锚点导航、倒计时状态位这类依赖首页章节与 JS 的东西一律不带到新页。
    失败一律返回空 dict → 回退通用模板；这是增强不是硬依赖，老站没有这些标记照样建页。
    """
    index = work / site.asset_dir / "index.html"
    if not index.is_file():
        return {}
    try:
        html = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    styles = re.findall(r'<link[^>]+rel=["\']stylesheet["\'][^>]*>', html, flags=re.I)
    theme = re.findall(r'<meta[^>]+name=["\']theme-color["\'][^>]*>', html, flags=re.I)
    icon = re.findall(r'<link[^>]+rel=["\']icon["\'][^>]*>', html, flags=re.I)
    header = re.search(r"<header\b[^>]*>[\s\S]*?</header>", html, flags=re.I)
    footer = re.search(r"<footer\b[^>]*>[\s\S]*?</footer>", html, flags=re.I)
    return {
        "styles": "\n".join(dict.fromkeys(styles)),
        "theme": "\n".join(dict.fromkeys(theme)),
        "icon": "\n".join(dict.fromkeys(icon)),
        "header": _header_brand(header.group(0), site.brand) if header else "",
        "footer": footer.group(0) if footer else "",
        # 新骨架站的标记：有全局 site.css 才启用卡片化内页，老站外观一律不动。
        "enhanced": "/assets/site.css" in html,
    }


def _enhanced_content(site: SiteConfig, page: dict[str, Any]) -> str:
    """v2 骨架内容区：面包屑 + 导语 + 目录 + 卡片章节 + FAQ 卡片 + 返回首页。"""
    h1 = esc(page.get("h1") or page["title"])
    intro = esc(page.get("intro", ""))
    toc, blocks = [], []
    for i, sec in enumerate(page.get("sections", []), 1):
        heading = esc(sec.get("heading", ""))
        anchor = f"sec-{i}"
        toc.append(f'<a href="#{anchor}">{heading}</a>')
        paras = "".join(f"<p>{esc(p)}</p>" for p in sec.get("paragraphs", []))
        blocks.append(f'<section class="section-card" id="{anchor}">'
                      f'<h2>{heading}</h2>{paras}</section>')
    toc_html = (f'<nav class="toc"><span class="toc-title">On this page</span>'
                + "".join(toc) + "</nav>") if len(toc) > 1 else ""
    faq_html = "".join(
        f'<div class="faq-item"><h3>{esc(item["q"])}</h3><p>{esc(item["a"])}</p></div>'
        for item in page.get("faq", [])
    )
    faq = (f'<section class="faq-section"><h2 class="faq-head">Frequently asked questions</h2>'
           f'{faq_html}</section>') if faq_html else ""
    return f"""
  <nav class="crumbs"><a href="/">{esc(site.brand)}</a><span>/</span><span>{h1}</span></nav>
  <div class="lede"><span class="tag">Guide</span><h1>{h1}</h1><p class="intro">{intro}</p></div>
  {toc_html}
  {''.join(blocks)}
  {faq}
  <p class="back"><a href="/">&larr; Back to {esc(site.brand)}</a></p>"""


def _render_with_skin(site: SiteConfig, page: dict[str, Any], skin: dict[str, str]) -> str:
    """同站风格版：套首页 stylesheet / theme / header / footer，内容区只补最小样式。"""
    sections = []
    for sec in page.get("sections", []):
        paras = "".join(f"<p>{esc(p)}</p>" for p in sec.get("paragraphs", []))
        sections.append(f"<h2>{esc(sec.get('heading', ''))}</h2>{paras}")
    faq_html = "".join(
        f"<h3>{esc(item['q'])}</h3><p>{esc(item['a'])}</p>"
        for item in page.get("faq", [])
    )
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Article", "headline": page["title"],
             "description": page["description"],
             "mainEntityOfPage": canonical_url(site, page["slug"])},
            {"@type": "FAQPage", "mainEntity": [
                {"@type": "Question", "name": item["q"],
                 "acceptedAnswer": {"@type": "Answer", "text": item["a"]}}
                for item in page.get("faq", [])
            ]},
        ],
    }, ensure_ascii=False)
    enhanced = bool(skin.get("enhanced"))
    head_extra = "\n".join(x for x in (skin.get("theme"), skin.get("icon"),
                                        skin.get("styles"),
                                        _SKIN_CSS_V2 if enhanced else _SKIN_CSS) if x)
    header = skin.get("header") or f'<header><a href="/">{esc(site.brand)}</a></header>'
    footer = skin.get("footer") or (
        f'<footer>{esc(site.brand)} · <a href="/privacy.html">Privacy</a> · '
        '<a href="/terms.html">Terms</a></footer>')
    content = _enhanced_content(site, page) if enhanced else (
        f'<h1>{esc(page.get("h1") or page["title"])}</h1>'
        f'<p>{esc(page.get("intro", ""))}</p>'
        f"{''.join(sections)}"
        f'<section class="faq agent-faq"><h2>FAQ</h2>{faq_html}</section>')
    return f"""<!DOCTYPE html>
<html lang="{esc(site.lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(page['title'])}</title>
<meta name="description" content="{esc(page['description'])}">
<link rel="canonical" href="{canonical_url(site, page['slug'])}">
<meta property="og:title" content="{esc(page['title'])}">
<meta property="og:description" content="{esc(page['description'])}">
<meta property="og:type" content="article">
<script type="application/ld+json">{jsonld}</script>
{head_extra}
</head>
<body>
{header}
<main class="wrap agent-page">{content}
</main>
{footer}
</body>
</html>
"""


def render_page(site: SiteConfig, page: dict[str, Any], skin: dict[str, str] | None = None) -> str:
    """渲染一页。给了 skin（从站点首页抽的骨架）就走同站风格，否则用通用模板。"""
    if skin:
        return _render_with_skin(site, page, skin)
    tpl = (TEMPLATE_DIR / "site-page.html").read_text(encoding="utf-8")
    sections = []
    for sec in page.get("sections", []):
        paras = "".join(f"<p>{esc(p)}</p>" for p in sec.get("paragraphs", []))
        sections.append(f"<h2>{esc(sec.get('heading', ''))}</h2>{paras}")

    faq_html = "".join(
        f"<h3>{esc(item['q'])}</h3><p>{esc(item['a'])}</p>"
        for item in page.get("faq", [])
    )
    jsonld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Article", "headline": page["title"],
             "description": page["description"],
             "mainEntityOfPage": canonical_url(site, page["slug"])},
            {"@type": "FAQPage", "mainEntity": [
                {"@type": "Question", "name": item["q"],
                 "acceptedAnswer": {"@type": "Answer", "text": item["a"]}}
                for item in page.get("faq", [])
            ]},
        ],
    }
    tokens = {
        "lang": site.lang, "brand": site.brand, "host": site.host,
        "slug": page["slug"], "title": esc(page["title"]),
        "description": esc(page["description"]),
        "h1": esc(page.get("h1") or page["title"]),
        "intro": esc(page.get("intro", "")), "sections": "\n".join(sections),
        "faq": faq_html, "jsonld": json.dumps(jsonld, ensure_ascii=False),
    }
    for key, value in tokens.items():
        tpl = tpl.replace("{{" + key + "}}", value)
    return tpl


def _write_page(work: Path, site: SiteConfig, page: dict[str, Any],
                skin: dict[str, str] | None = None) -> None:
    public = work / site.asset_dir
    public.mkdir(exist_ok=True)
    (public / f"{page['slug']}.html").write_text(
        render_page(site, page, skin), encoding="utf-8")


def _update_sitemap(work: Path, site: SiteConfig, slug: str) -> None:
    """兜底静态文件（新站 scaffold 才有）；线上真源是 tool-proxy 的 SITE_PATHS。

    这里只保证万一被直接访问时 URL 口径正确，不再承担「新页进 sitemap」的职责——
    那一步由建页后自动登记代理配置完成。
    """
    sitemap = work / site.asset_dir / "sitemap.xml"
    if not sitemap.exists():
        return
    text = sitemap.read_text(encoding="utf-8")
    loc = f"<loc>{canonical_url(site, slug)}</loc>"
    if loc in text:
        return
    entry = f"  <url>{loc}<changefreq>monthly</changefreq></url>\n"
    sitemap.write_text(text.replace("</urlset>", entry + "</urlset>"), encoding="utf-8")


def _page_cards(work: Path, site: SiteConfig) -> str:
    """从站点已有文章页生成首页卡片；标题/摘要从页面自己的 meta 读，不另建索引文件。"""
    public = work / site.asset_dir
    skip = {"index.html", "privacy.html", "terms.html", "404.html"}
    cards = []
    for f in sorted(public.glob("*.html")):
        if f.name in skip:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        title = re.search(r"<title>([\s\S]*?)</title>", text, re.I)
        desc = (re.search(r'<meta\s+name=["\']description["\'][^>]*content=["\'](.*?)["\']',
                          text, re.I | re.S)
                or re.search(r'<meta\s+[^>]*content=["\'](.*?)["\'][^>]*name=["\']description["\']',
                             text, re.I | re.S))
        t = esc(html_mod.unescape(title.group(1).strip())) if title else esc(f.stem.replace("-", " ").title())
        d = esc(html_mod.unescape(desc.group(1).strip())) if desc else ""
        cards.append(f'<a class="page-card" href="/{f.stem}"><span class="card-eyebrow">Guide</span>'
                     f'<h3>{t}</h3><p>{d}</p></a>')
    if not cards:
        return ('<article class="page-card"><span class="card-eyebrow">Coming soon</span>'
                '<h3>Guides are being written</h3>'
                '<p>Published pages will appear here automatically.</p></article>')
    return "\n      ".join(cards)


def _refresh_index(work: Path, site: SiteConfig) -> None:
    """只更新新骨架首页的文章卡片区；没有标记的站点首页一律不碰。"""
    index = work / site.asset_dir / "index.html"
    if not index.is_file():
        return
    html_text = index.read_text(encoding="utf-8", errors="replace")
    if PAGES_START not in html_text or PAGES_END not in html_text:
        return
    pattern = re.escape(PAGES_START) + r"[\s\S]*?" + re.escape(PAGES_END)
    cards = _page_cards(work, site)
    updated = re.sub(pattern,
                     lambda _m: f"{PAGES_START}\n      {cards}\n    {PAGES_END}",
                     html_text, count=1)
    if updated != html_text:
        index.write_text(updated, encoding="utf-8")


def render_files(site: SiteConfig, page: dict[str, Any]) -> dict[str, Any]:
    """渲染 + 兜底 sitemap + 首页卡片 + git add。每一步由 tool 层单独幂等。"""
    slug = page.get("slug", "")
    if not SLUG_RE.match(slug):
        raise RuntimeError(f"非法 slug: {slug!r}")
    work = ensure_repo(site)
    # ★同站风格继承：新页套首页的 stylesheet / theme / header / footer，
    # 不再拿通用浅色模板硬塞进深色站（黑五页事故的第二个根因）。
    _write_page(work, site, page, _site_skin(work, site))
    _update_sitemap(work, site, slug)
    _refresh_index(work, site)
    git(work, "add", "-A")
    return {"ok": True, "work": str(work), "slug": slug,
            "changed": has_staged_changes(work), "repo": site.repo,
            "branch": site.branch}
