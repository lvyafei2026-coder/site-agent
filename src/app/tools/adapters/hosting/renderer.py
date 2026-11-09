# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""页面渲染与 sitemap：只动本地文件，可逆。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.app.config import TEMPLATE_DIR, SiteConfig
from .repository import ensure_repo, git, has_staged_changes

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")


def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render_page(site: SiteConfig, page: dict[str, Any]) -> str:
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
             "mainEntityOfPage": f"https://{site.host}/{page['slug']}.html"},
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


def _write_page(work: Path, site: SiteConfig, page: dict[str, Any]) -> None:
    public = work / site.asset_dir
    public.mkdir(exist_ok=True)
    (public / f"{page['slug']}.html").write_text(render_page(site, page), encoding="utf-8")


def _update_sitemap(work: Path, site: SiteConfig, slug: str) -> None:
    sitemap = work / site.asset_dir / "sitemap.xml"
    if not sitemap.exists():
        return
    text = sitemap.read_text(encoding="utf-8")
    loc = f"<loc>https://{site.host}/{slug}.html</loc>"
    if loc in text:
        return
    entry = f"  <url>{loc}<changefreq>monthly</changefreq></url>\n"
    sitemap.write_text(text.replace("</urlset>", entry + "</urlset>"), encoding="utf-8")


def render_files(site: SiteConfig, page: dict[str, Any]) -> dict[str, Any]:
    """渲染 + 更新 sitemap + git add。每一步由 tool 层单独幂等。"""
    slug = page.get("slug", "")
    if not SLUG_RE.match(slug):
        raise RuntimeError(f"非法 slug: {slug!r}")
    work = ensure_repo(site)
    _write_page(work, site, page)
    _update_sitemap(work, site, slug)
    git(work, "add", "-A")
    return {"ok": True, "work": str(work), "slug": slug,
            "changed": has_staged_changes(work), "repo": site.repo,
            "branch": site.branch}
