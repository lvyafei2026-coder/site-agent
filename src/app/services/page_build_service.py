# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用；详见 LICENSE.

"""建页线的纯业务：取词 / 归类 / 解析方案正文成页面结构。

由 nodes/page_nodes.py 调用，与热词线的 hotword_service 对称：
工具步骤（render / push / verify）经 tool_service 分发到 registry，这里的三个函数
不进 registry——它们是「LLM 与判据」这类横切能力，不是工具。

★解析是「尽力而为 + 确定性兜底」：ai:v3 正文的 8 小节只认「数字) 标题」的形状、
  不写死节名（节名可变，verdict_of 同一原则）；一节都认不出来就整篇当单节，
  解析永不抛错——建页失败必须落在明确的一步（render / push / verify），不能坏在解析。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.app.config import SITES
from src.app.enums import ClassifyBy, CurationStatus, Intent, PageQueryMode
from src.app.services.extraction_service import extraction_service
from src.app.tools.adapters.cloudflare import database
from src.app.tools.adapters.cloudflare.kv import AI_CACHE_PREFIX, SAVED_KEY, get_kv, term_key
from src.app.tools.adapters.cloudflare.sql import PAGE_QUERIES
from src.app.utils.kv_shape import kv_list

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NUM_HEADING = re.compile(r"(?:^|\n)\s*\d+\)\s*([^\n]{2,40})\n?")
_MAX_PARAS = 6          # 每节最多几段
_MAX_SECTIONS = 8       # 最多几节
_MAX_PARA_CHARS = 400   # 单段字符上限（超出截断，防一节灌爆页面）


def slugify(term: str) -> str:
    """热词 → 页面 slug：小写连字符，60 字符收口。空词给占位（render 侧还会再校验）。"""
    return (_SLUG_RE.sub("-", term_key(term)).strip("-") or "page")[:60].strip("-") or "page"


def _paragraphs(block: str) -> list[str]:
    out: list[str] = []
    for raw in re.split(r"\n\s*\n", str(block or "")):
        p = re.sub(r"\s+", " ", raw).strip().lstrip("-•* ").strip()
        if len(p) < 20:            # 标题行、空行、过短的碎片不当正文段
            continue
        out.append(p[:_MAX_PARA_CHARS])
        if len(out) >= _MAX_PARAS:
            break
    return out


def parse_page(term: str, text: str) -> dict[str, Any]:
    """ai:v3 方案正文 → 页面结构。任何输入都返回可用 dict，不抛错。

    切分按「数字) 节标题」的形状；认不出节就整篇单节。description / intro 取正文
    开头，FAQ 正文里没有结构化来源，恒为空（模板对空 FAQ 的渲染已验证）。
    """
    body = str(text or "").strip()
    paras_all = _paragraphs(body)
    intro = paras_all[0] if paras_all else f"About {term}."

    sections: list[dict[str, Any]] = []
    parts = _NUM_HEADING.split(body)
    # split 带捕获组：[前文, 标题1, 正文1, 标题2, 正文2, ...]
    for i in range(1, len(parts) - 1, 2):
        heading = re.sub(r"\s+", " ", parts[i]).strip().strip("：:")
        paras = _paragraphs(parts[i + 1])
        if heading and paras:
            sections.append({"heading": heading[:40], "paragraphs": paras})
    if not sections:               # 兜底：整篇单节，标题用热词本身
        if paras_all:
            sections.append({"heading": term_key(term)[:40] or "Overview",
                             "paragraphs": paras_all})
    sections = sections[:_MAX_SECTIONS]

    return {
        "slug": slugify(term),
        "title": (term_key(term).title() or term)[:70],
        "description": (intro or f"Guide to {term}.")[:160],
        "h1": (term_key(term).title() or term)[:70],
        "intro": intro,
        "sections": sections,
        "faq": [],
    }


def _site_from_rule(term: str) -> str:
    """规则兜底：热词命中站点名 / brand 词根就归它（LLM 不可用或归错时用）。"""
    t = term_key(term)
    for site in SITES.values():
        tokens = {term_key(site.name), *term_key(site.brand).split()}
        tokens = {x for x in tokens if len(x) >= 4}
        if any(tok and tok in t for tok in tokens):
            return site.name
    return ""


def classify_site(term: str, text: str = "") -> dict[str, Any]:
    """把热词归类到已有子站。LLM（extraction 的 target_site）优先，规则兜底。

    返回 {site, by}：site 为空串 = 归不进任何已有站 → 走「建新站」（本轮跳过）。
    LLM 的输入是站点清单 + 热词 + 方案摘要，不是用户原始请求——归类只看这三样。
    """
    sites_line = "；".join(f"{s.name}({s.brand})" for s in SITES.values())
    brief = re.sub(r"\s+", " ", str(text or ""))[:300]
    # ★措辞不得复刻 D1 里受管模板的行（test_prompts 守卫）：这段是发给
    #   extraction 的归类请求，不是提示词模板——字段名换成自有叫法。
    ask = (f"站点清单：{sites_line}\n待归类词：{term}\n方案摘要：{brief}\n"
           "只输出清单里最匹配的站点名；都不合适就输出空。")
    extracted = extraction_service.extract(ask, Intent.WORD_PLAN_PAGE.value)
    site = extracted.get("target_site", "").strip()
    by = ClassifyBy.LLM.value
    if site not in SITES:
        site = _site_from_rule(term)
        by = ClassifyBy.RULE.value
    if site and site not in SITES:     # 兜底结果也必须合法，绝不信 LLM 的自由文本
        site, by = "", ClassifyBy.NONE.value
    return {"site": site, "by": by}


def _planned_rows(limit: int = 5) -> list[dict[str, Any]]:
    """排产入选待建的行，按写入顺序（= 排产给的优先级）返回。

    D1 不可达时返回空列表：建页线回退到待开发池顺序，不因为台账读不到就停摆。
    """
    query = PAGE_QUERIES[PageQueryMode.PLANNED.value]
    try:
        return database.query(f"{query.sql} LIMIT ?", [limit])
    except Exception:  # noqa: BLE001 - 台账不可读按无计划处理
        return []


def pick_word(term: Optional[str] = None) -> Optional[dict[str, Any]]:
    """取一条要建页的词，连带它的 ai:v3 方案正文与排产给的目标站点。

    显式给 term 就只查该词；否则**先读排产计划**（agent_pages 里 status=planned
    的行，写入顺序即优先级顺序），计划里有带正文的词就用它和它定的站点；
    计划为空或都没正文时回退 saved 表顺序自己找一条。
    没有正文的词跳过——没分析过的词不该建页（漏斗顺序 = 分析筛选 → 排产 → 建页）。
    找不到返回 None（本轮无事可做）。
    """
    kv = get_kv()
    if term:
        text = kv.get_text(AI_CACHE_PREFIX + term_key(term))
        return {"term": term, "text": text} if text else None
    for row in _planned_rows():
        t = str(row.get("keyword") or "")
        if not t:
            continue
        text = kv.get_text(AI_CACHE_PREFIX + term_key(t))
        if text:
            return {"term": t, "text": text, "site": str(row.get("site") or "")}
    for row in kv_list(kv, SAVED_KEY):
        if not isinstance(row, dict) or row.get("status") != CurationStatus.TODO.value:
            continue
        t = str(row.get("term") or "")
        text = kv.get_text(AI_CACHE_PREFIX + term_key(t))
        if text:
            return {"term": t, "text": text}
    return None


def todo_pool_size() -> int:
    """待开发池大小（汇总/日志用）。读失败按 0，不阻塞建页。"""
    try:
        return sum(1 for r in kv_list(get_kv(), SAVED_KEY)
                   if isinstance(r, dict) and r.get("status") == CurationStatus.TODO.value)
    except Exception:  # noqa: BLE001 - KV 漂移时汇总口径失败不该打断主流程
        return 0
