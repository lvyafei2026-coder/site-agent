# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""建页正文写作：内部选词分析稿 → 访客可读的页面正文。

ai:v3 是「这个词值不值得做」的决策记录（词义 / 建议站型 / 变现 / 结论），不是给
访客看的文案。直接拿它渲染，页面上就会出现「变现：affiliate 为主」这类内部判断，
而且按「数字) 」机械切分还会把整句截成半句。这一步让 LLM 把分析稿当素材重写，
代码只做两件事：洗字段（保证 renderer 拿到的都是可用结构）+ 拦元话语。

★别名 vs 拦截：写作失败（无 key / 调用异常 / 结构不合格）不报错，仍回退解析稿让
  预览能看；但回退稿一旦含内部分析用语，analysis_leak() 会把渲染直接拦下——宁可
  这一页不建，也不能把决策稿推到线上（2026-11-25 黑五页事故）。
"""

from __future__ import annotations

import json
from typing import Any

from src.app.config import SITES
from src.app.enums import PromptTask
from src.app.llm.completion import complete_json
from src.app.observability.langsmith import traceable
from src.app.services.prompt_service import prompt_service
from src.app.utils.logger import get_logger

log = get_logger(__name__)

MAX_ANALYSIS_CHARS = 4000   # 分析稿进提示词的上限：8 小节正文，超长只会稀释写作要求
MAX_SITE_BRIEF_CHARS = 2600 # 原站摘要上限：给写手"已有什么 + 风格"，不能盖过分析稿
MAX_SECTIONS = 6
MAX_SECTION_PARAS = 4
MAX_FAQ = 6
MAX_TITLE_CHARS = 90
MAX_DESC_CHARS = 200
MAX_INTRO_CHARS = 700
MAX_PARA_CHARS = 900

# 分析稿的特征用语：出现在成稿的 title/description/intro/sections/faq 里，说明这一页
# 还是决策记录而不是页面内容。只认这些短语，不做「中文/英文」这类粗判——真页面里
# 出现中文段落的可能性远低于误杀风险，判据要窄且可解释。
ANALYSIS_MARKERS = (
    "词义与意图", "关联词解读", "建议站型", "首页模块", "信号标签",
    "差异化：", "变现：", "结论：", "选词分析", "目标关键词",
)


def _text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _paragraphs(raw: object) -> list[str]:
    """段落数组洗成非空字符串；模型偶尔把整节写成一整段字符串，就按空行再切。"""
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for item in items:
        for piece in str(item or "").split("\n"):
            p = _text(piece, MAX_PARA_CHARS)
            if len(p) >= 2:
                out.append(p)
        if len(out) >= MAX_SECTION_PARAS:
            break
    return out[:MAX_SECTION_PARAS]


def normalize_page(raw: dict, term: str) -> dict[str, Any] | None:
    """模型输出 → renderer 认的页面结构（不含 slug，slug 由词决定）。

    节/段/FAQ 全部洗完仍没有一节正文就返回 None：那说明模型没按约定产出，交给上层
    回退解析稿，别拿一个空壳覆盖掉预览。
    """
    data = raw
    if not isinstance(data.get("sections"), list):
        inner = data.get("page")
        if isinstance(inner, dict):
            data = inner

    sections: list[dict[str, Any]] = []
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        heading = _text(sec.get("heading"), 120)
        paras = _paragraphs(sec.get("paragraphs"))
        if heading and paras:
            sections.append({"heading": heading, "paragraphs": paras})
        if len(sections) >= MAX_SECTIONS:
            break

    faq = []
    for item in data.get("faq") or []:
        if not isinstance(item, dict):
            continue
        q = _text(item.get("q") or item.get("question"), 200)
        a = _text(item.get("a") or item.get("answer"), 400)
        if q and a:
            faq.append({"q": q, "a": a})
        if len(faq) >= MAX_FAQ:
            break

    if not sections:
        return None

    title = _text(data.get("title"), MAX_TITLE_CHARS) or term
    return {
        "title": title,
        "description": _text(data.get("description"), MAX_DESC_CHARS),
        "h1": _text(data.get("h1"), MAX_TITLE_CHARS) or title,
        "intro": _text(data.get("intro"), MAX_INTRO_CHARS),
        "sections": sections,
        "faq": faq,
    }


def analysis_leak(page: dict[str, Any]) -> str:
    """成稿里残留的分析用语；没有返回空串。

    校验对象是「真正会被渲染的文字」，所以 intro / sections / faq 都要看——只查
    title 会让内文原样漏过去。
    """
    parts = [page.get("title"), page.get("description"), page.get("h1"), page.get("intro")]
    for sec in page.get("sections") or []:
        if isinstance(sec, dict):
            parts.append(sec.get("heading"))
            parts.extend(sec.get("paragraphs") or [])
    for item in page.get("faq") or []:
        if isinstance(item, dict):
            parts.append(item.get("q"))
            parts.append(item.get("a"))
    blob = "\n".join(str(p or "") for p in parts)
    for marker in ANALYSIS_MARKERS:
        if marker in blob:
            return marker
    return ""


def _payload(term: str, site_name: str, analysis: str, site_brief: str = "") -> str:
    site = SITES[site_name]
    # ★原站摘要并进 analysis 正文而不只是单独字段：D1 提示词只认识 analysis，
    # 单开字段模型未必会读；拼进来它就得按"素材"处理，写出的页才不撞原站。
    source = analysis[:MAX_ANALYSIS_CHARS]
    if site_brief:
        source = f"{source}\n\n[原站现状与写作约束]\n{site_brief[:MAX_SITE_BRIEF_CHARS]}"
    return json.dumps(
        {"term": term,
         "site": {"name": site.name, "brand": site.brand, "lang": site.lang},
         "analysis": source,
         "site_brief": site_brief[:MAX_SITE_BRIEF_CHARS]},
        ensure_ascii=False,
    )


class PageContentService:
    @traceable(name="write_page_content")
    def write(self, term: str, site_name: str, analysis: str,
              versions: dict[str, str] | None = None,
              site_brief: str = "") -> tuple[dict[str, Any] | None, str]:
        """按访客口吻重写正文；失败返回 (None, 原因)，原因原样进建页失败信息。

        不抛错：写作是增强步骤，LLM 不可用只应让这一页退化成草稿，不该让建页线
        整轮崩掉。草稿是否可渲染由 analysis_leak() 在节点里判定；把失败原因带回
        节点，是为了让「解析稿被拦下」时能直接说清是提示词、LLM 还是结构问题。
        """
        if not term or site_name not in SITES or not str(analysis or "").strip():
            return None, "词 / 站点 / 分析稿缺失"
        versions = versions or {}
        try:
            system = prompt_service.load_prompt(
                PromptTask.PAGE_CONTENT.value,
                version=versions.get(PromptTask.PAGE_CONTENT.value))
        except RuntimeError as exc:
            # D1 还没建这个 task（或凭据失效）：当写作失败处理，回退解析稿由节点校验。
            log.warning("page_content 提示词加载失败：%s", str(exc)[:200])
            return None, f"page_content 提示词读不到（{str(exc)[:160]}）"
        data, failed, _retryable = complete_json(
            system, _payload(term, site_name, str(analysis), site_brief),
            warn_event="page_content_failed")
        if not isinstance(data, dict):
            if failed:
                return None, "LLM 调用失败（额度 / 鉴权 / 网络，日志 warn_event=page_content_failed）"
            return None, "LLM 没返回可解析的页面 JSON（日志 llm_nonjson_response）"
        page = normalize_page(data, term)
        if page is None:
            return None, "LLM 输出的页面结构不合格（没有任何带正文的小节）"
        return page, ""


page_content_service = PageContentService()
