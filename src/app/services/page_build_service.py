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

import json
import re
from typing import Any, Optional

from src.app.config import SITES
from src.app.enums import ClassifyBy, CurationStatus, Intent, PageQueryMode
from src.app.services.extraction_service import extraction_service
from src.app.tools.adapters.cloudflare import database
from src.app.utils.term_key import term_key
from src.app.tools.adapters.cloudflare.sql import PAGE_QUERIES, Sql

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
    """规则兜底：热词命中站点名 / brand 词根 / 语义别名就归它（LLM 不可用或归错时用）。

    ★别名按整串短语匹配（term_key 后整串 in）：beaver-moon 的别名 "november full moon"
      能整串命中该词；单词级 "moon"/"full" 不拆开做 token，否则会误伤 clocksgoback
      的 full moon 页。站点名/brand 仍按原词根匹配（≥4 字符），兼容既有归类。
    """
    t = term_key(term)
    for site in SITES.values():
        tokens = {term_key(site.name), *term_key(site.brand).split()}
        tokens = {x for x in tokens if len(x) >= 4}
        if any(tok and tok in t for tok in tokens):
            return site.name
        if any(term_key(alias) in t for alias in site.aliases if term_key(alias)):
            return site.name
    return ""


def valid_site(name: str) -> str:
    """台账或调用方给的站点名，只认已登记的子站；非法一律当「没给」（回退现场归类）。

    排产与建页是两条线，台账里可能留着改过名/删掉的站，不能拿它直接去渲染。
    """
    return name if name in SITES else ""


def classify_site(term: str, text: str = "") -> dict[str, Any]:
    """把热词归类到已有子站。LLM（extraction 的 target_site）优先，规则兜底。

    返回 {site, by}：site 为空串 = 归不进任何已有站 → 走「建新站」（本轮跳过）。
    LLM 的输入是站点清单 + 热词 + 方案摘要，不是用户原始请求——归类只看这三样。
    """
    # brand 是访客可见站名，别名是归类线索（如 beaver-moon 接 november full moon）。
    # 两者一起给 LLM，避免它只按站名面值判断、把语义相近的词误判成「无匹配站」。
    sites_line = "；".join(
        f"{s.name}({s.brand}{'；别名：' + '、'.join(s.aliases) if s.aliases else ''})"
        for s in SITES.values())
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


def _planned_rows() -> list[dict[str, Any]]:
    query = PAGE_QUERIES[PageQueryMode.PLANNED.value]
    return database.query(f"{query.sql} LIMIT 1000")


def _analysis_text(term: str) -> str:
    rows = database.query(
        """SELECT a.result_json
           FROM radar_term_analyses a
           JOIN radar_terms t ON t.term_id = a.term_id
           WHERE t.term_key = ? AND a.status = 'completed'
           LIMIT 1""",
        [term_key(term)],
    )
    if not rows or not rows[0].get("result_json"):
        return ""
    return str(json.loads(rows[0]["result_json"]).get("text") or "")


def pick_word(term: Optional[str] = None) -> Optional[dict[str, Any]]:
    """从 planned 台账或 todo 状态中取一条有完整分析正文的词。"""
    if term:
        text = _analysis_text(term)
        return {"term": term, "text": text} if text else None

    for row in _planned_rows():
        keyword = str(row.get("keyword") or "")
        if not keyword:
            continue
        text = _analysis_text(keyword)
        if text:
            return {"term": keyword, "text": text, "site": str(row.get("site") or "")}

    rows = database.query(
        """SELECT t.term
           FROM radar_term_status s
           JOIN radar_terms t ON t.term_id = s.term_id
           JOIN radar_term_analyses a ON a.term_id = t.term_id
           WHERE s.status = 'todo' AND a.status = 'completed'
           ORDER BY s.updated_at ASC LIMIT 1"""
    )
    if not rows:
        return None
    keyword = str(rows[0]["term"])
    text = _analysis_text(keyword)
    return {"term": keyword, "text": text} if text else None


def retarget_planned_site(term: str, text: str = "") -> dict[str, Any]:
    """重新分析后重判目标站；只更新已有的 planned 行。"""
    body = text or _analysis_text(term)
    cls = classify_site(term, body)
    site = str(cls.get("site") or "")
    out = {
        "changed": False,
        "site": site,
        "by": str(cls.get("by") or ClassifyBy.NONE.value),
        "term": term,
    }
    if not site:
        out["reason"] = "未匹配到已有站，保留新建站方案"
        return out
    database.execute(Sql.RETARGET_PLANNED_SITE, [site, term])
    out["changed"] = True
    return out


def todo_pool_size() -> int:
    rows = database.query(
        "SELECT COUNT(*) AS count FROM radar_term_status WHERE status = 'todo'"
    )
    return int(rows[0]["count"]) if rows else 0
