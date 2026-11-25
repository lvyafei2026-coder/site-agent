# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用；详见 LICENSE.

"""站情通读：建新页前先读目标站已有的页面，判重 + 定增强/新增 + 给写作喂风格。

背景（2026-11-25 黑五页事故）：建页线只拿 ai:v3 分析稿写新页，不知道目标站
已经有一篇 `is-it-black-friday-today`、首页又覆盖了大半日期与 FAQ，结果同一站
两页内容重复、风格也不一致（新页走通用浅色模板，首页是深色 + 倒计时）。

本服务把「通读原站」变成建页链的一步，全部是确定性代码（不额外烧 LLM）：
    read_site()      读仓库 public/*.html → 每页 title / 正文 / 标题 / FAQ / 样式基调
    audit()          新词与已有页比对 → add / enhance / skip + 重合证据
    style_brief()    从首页样式基调生成一段写作约束，塞进 page_content 的 payload

判重口径：词集合的 Jaccard（标题权重更高），不是子串匹配——同义改写也能认出
「这页已经讲过」。★只拦「已有页≡新词」与「新页原封不动重复」，不拦同站不同
角度的页：站群本来就是一族相关长尾，全按主题相似砍掉会把站建死。

读站失败（仓库没克隆 / 没 token / 目录空）一律降级为「无原站信息」，不阻塞建页；
但会把 ok=False + reason 带进结果面板，让人知道这轮没做通读。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.app.config import REPO_DIR, SITES, SiteConfig
from src.app.enums import AuditDecision
from src.app.services.page_build_service import slugify
from src.app.tools.adapters.publishing.github_repo import ensure_repo
from src.app.utils.logger import get_logger

log = get_logger(__name__)

# 判重参数：标题命中权重高，正文达到 0.55 才能算"整页重复"
TITLE_OVERLAP = 0.6
BODY_OVERLAP = 0.55
MIN_TITLE_WORDS = 2
MAX_PAGES = 60              # 一站最多读多少页（防超大站拖慢建页）
MAX_CHARS_PER_PAGE = 6000   # 单页正文截断：够判重，不把整站塞进 payload
MAX_BRIEF_CHARS = 2600      # 喂给写作的原站摘要上限

_STOP = frozenset(
    "a an the and or but if then than of to in on at for from by with as is are "
    "was were be been being it its this that these those you your we our they "
    "do does did can could will would should when where what which who how why "
    "not no yes into about over under more most some any all each every per via "
    "up out off again very just now new today".split()
)
_WORD = re.compile(r"[a-z][a-z0-9-]{1,}")
_TAG = re.compile(r"<[^>]+>")


def words(text: object) -> frozenset[str]:
    """英文词集合：小写、去停用词、连字符保留。中文分析稿按字切不在本服务范围。"""
    return frozenset(w for w in _WORD.findall(str(text or "").lower())
                     if w not in _STOP and len(w) > 2)


def overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard；两边任一为空返回 0（空词集不算"重复"）。"""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _plain(html: str) -> str:
    """HTML → 纯文本：去 script/style，标签换空格，压缩空白。"""
    text = re.sub(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", " ", html, flags=re.I)
    return " ".join(_TAG.sub(" ", text).split())


def _headings(html: str) -> list[str]:
    return [" ".join(m.split()) for m in re.findall(
        r"<h[12][^>]*>([\s\S]*?)</h[1-2]>", html, flags=re.I)]


def _meta(html: str, name: str) -> str:
    """取 <meta name=...> 或 <title>：原站的自我介绍比正文更能定风格。"""
    if name == "title":
        m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, flags=re.I)
    else:
        m = re.search(r'<meta[^>]+name=["\']' + re.escape(name) +
                      r'["\'][^>]+content=["\']([\s\S]*?)["\']', html, flags=re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([\s\S]*?)["\'][^>]+name=["\']' +
                          re.escape(name) + r'["\']', html, flags=re.I)
    return " ".join(m.group(1).split()) if m else ""


def _style_profile(index_html: str) -> dict[str, Any]:
    """从首页抽风格基调：主题变量、字体、标题/正文配色，用来约束新页文案口吻。

    只抽事实（CSS 里写了什么），不写死任何站点的值——换站、改版自动跟着变。
    """
    color = re.findall(r"(?:--(?:accent|ink|fg|bg|link|text)|color|background)\s*:\s*([^;}]{3,40})",
                       index_html, flags=re.I)
    # 深色判断：内联变量或 theme-color 任一命中即可（有的站把样式放外链 CSS，
    # head 里只有 theme-color 这一个可靠信号）。
    dark = bool(re.search(r"(?:--bg|theme-color[\"'][^>]*content)\s*[:=]\s*[\"']?\s*#(?:0|1)[0-9a-f]{5}",
                          index_html, flags=re.I))
    return {
        "dark": dark,
        "colors": list(dict.fromkeys(c.strip() for c in color))[:8],
        "has_countdown": "countdown" in index_html.lower() or "cd-timer" in index_html,
        "has_faq": "faq" in index_html.lower(),
        "title": _meta(index_html, "title"),
        "description": _meta(index_html, "description"),
    }


def read_site(site_name: str) -> dict[str, Any]:
    """通读目标站：返回 {ok, reason, pages, style, site}。任何失败都降级不抛错。

    先 ensure_repo 拿到与渲染阶段同一份仓库快照（幂等 fetch/reset，生产已有公共
    能力），再读 public/*.html——预览与上线看到的是同一份"原站现状"。
    """
    site: SiteConfig | None = SITES.get(site_name)
    if site is None:
        return {"ok": False, "reason": f"未知站点 {site_name}", "pages": [],
                "style": {}, "site": site_name}
    try:
        root = ensure_repo(site)
    except Exception as exc:  # noqa: BLE001 - clone 失败降级，不阻塞建页
        log.warning("site_audit_repo_failed", extra={"site": site_name, "error": str(exc)[:160]})
        root = REPO_DIR / site.repo_name
    public = root / site.asset_dir
    if not public.is_dir():
        return {"ok": False, "reason": f"仓库未就绪：{public} 不存在（先 fetch 站点仓库）",
                "pages": [], "style": {}, "site": site_name}
    pages: list[dict[str, Any]] = []
    style: dict[str, Any] = {}
    for path in sorted(public.glob("*.html")):
        if path.name in {"404.html", "privacy.html", "terms.html"}:
            continue
        try:
            html = path.read_text(encoding="utf-8", errors="replace")[:MAX_CHARS_PER_PAGE]
        except OSError as exc:
            log.warning("site_audit_read_failed", extra={"path": str(path), "error": str(exc)[:120]})
            continue
        slug = "" if path.name == "index.html" else path.stem
        title = _meta(html, "title") or slug or "Home"
        page = {
            "slug": slug, "title": title,
            "description": _meta(html, "description"),
            "headings": _headings(html)[:12],
            "text": _plain(html)[:MAX_CHARS_PER_PAGE],
        }
        pages.append(page)
        if not slug:
            style = _style_profile(html)
        if len(pages) >= MAX_PAGES:
            break
    return {"ok": bool(pages), "reason": "" if pages else "站点没有任何可读 HTML 页面",
            "pages": pages, "style": style, "site": site_name}


def audit(term: str, site_digest: dict[str, Any]) -> dict[str, Any]:
    """新词 vs 已有页：给出 add / enhance / skip 与证据。

    - skip：某已有页的标题几乎就是这个词（Jaccard ≥ 0.6 且新词至少 2 个实词），
      说明这页已经建过，再建就是同义重复——直接拦下。
    - enhance：标题不完全相同但正文高度重合（≥ 0.55）——不改已上线页，改成在
      这页基础上做增量，写作时把已有内容喂进去，让新页只写缺口。
    - add：没撞车——正常新增，但带上原站风格与各页主题，让新页不像外来的。

    ★dry-run 与 confirm 都跑这一步：预览就能看到"这词已建过"，不用等推送才发现。
    """
    q = words(term)
    if not site_digest.get("ok") or not site_digest.get("pages"):
        return {"decision": AuditDecision.ADD.value, "reason": "未能通读原站，跳过判重",
                "matched": "", "score": 0.0, "confidence": "none"}
    # ★同 slug 已存在 = 这页已经建过：最硬的重复证据，不靠相似度猜。
    target = slugify(term)
    for page in site_digest["pages"]:
        if str(page.get("slug") or "") == target:
            return {"decision": AuditDecision.SKIP.value,
                    "reason": f"原站已有同 slug 页面 {target}.html（{page.get('title') or ''}），"
                              "再建只会覆盖/重复；请改走增强原页",
                    "matched": target, "score": 1.0, "confidence": "slug"}
    # 两个独立信号：标题相似度（判「这页建过了」）、正文覆盖率（判「正文已讲过这个词」）。
    # 正文用覆盖率而不是 Jaccard——原页通常长得多，Jaccard 对短词天然偏低。
    best_title: tuple[float, dict[str, Any]] = (0.0, {})
    best_body: tuple[float, dict[str, Any]] = (0.0, {})
    for page in site_digest["pages"]:
        title_score = overlap(
            q, words(page.get("title")) | words(" ".join(page.get("headings") or [])))
        body = words(page.get("text"))
        body_score = len(q & body) / len(q) if q else 0.0
        if title_score > best_title[0]:
            best_title = (title_score, page)
        if body_score > best_body[0]:
            best_body = (body_score, page)

    title_score, title_page = best_title
    if len(q) >= MIN_TITLE_WORDS and title_score >= TITLE_OVERLAP:
        return {"decision": AuditDecision.SKIP.value,
                "reason": (f"与已有页「{title_page.get('title') or '首页'}」标题相似度 "
                           f"{title_score:.0%}——这词已经建过，请改走「增强原页」而不是新增"),
                "matched": str(title_page.get("slug") or ""),
                "score": round(title_score, 3), "confidence": "title"}

    body_score, body_page = best_body
    if body_score >= BODY_OVERLAP:
        return {"decision": AuditDecision.ENHANCE.value,
                "reason": (f"已有页「{body_page.get('title') or '首页'}」正文覆盖该词 "
                           f"{body_score:.0%}——新页只写原站缺的部分"),
                "matched": str(body_page.get("slug") or ""),
                "score": round(body_score, 3), "confidence": "body"}

    matched = title_page if title_score >= body_score else body_page
    score = max(title_score, body_score)
    return {"decision": AuditDecision.ADD.value,
            "reason": f"原站 {len(site_digest['pages'])} 页，与该词最高相似度 {score:.0%}，可新增",
            "matched": str(matched.get("slug") or ""),
            "score": round(score, 3), "confidence": "add"}


def style_brief(site_digest: dict[str, Any], audit: dict[str, Any]) -> str:
    """原站摘要 → 一段写作约束，作为 page_content payload 的一部分（不是提示词模板）。

    只陈述原站事实与判重结论，不含完整提示词——措辞真源仍在 D1。写手看到这段
    自然知道该站已有什么、该补什么、该用哪种语气。
    """
    if not site_digest.get("ok"):
        return ""
    pages = site_digest.get("pages") or []
    lines = [f"目标站点已上线 {len(pages)} 页，新增页必须与原站风格一致、不重复："]
    for page in pages[:12]:
        items = "; ".join((page.get("headings") or [])[:6]) or (page.get("description") or "")[:90]
        lines.append(f"- {page.get('slug') or '/'} 《{page.get('title')}》：{items[:160]}")
    style = site_digest.get("style") or {}
    if style:
        lines.append("原站风格基调：" + ("深色" if style.get("dark") else "浅色") +
                     "；强调色 " + ", ".join(style.get("colors") or [])[:90] +
                     ("；首页有倒计时" if style.get("has_countdown") else "") +
                     ("；首页有 FAQ" if style.get("has_faq") else ""))
        if style.get("title"):
            lines.append(f"首页标题：{style['title'][:100]}")
    decision = str(audit.get("decision") or "")
    if decision == AuditDecision.ENHANCE.value:
        lines.append(f"★判重结论：{audit.get('reason', '')}。本次只写原站尚未覆盖的角度，"
                     "不要复述上面已有的标题、日期表或 FAQ。")
    elif decision == AuditDecision.SKIP.value:
        lines.append(f"★判重结论：{audit.get('reason', '')}。")
    text = "\n".join(lines)
    return text[:MAX_BRIEF_CHARS]
