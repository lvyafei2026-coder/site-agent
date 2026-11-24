# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""外链推广（find_backlinks 专属的共用能力）：取数结果 → LLM 起稿 Dev.to 文章 → 发布。

由 processing_service 在取数成功后调用。发布与写工具 publish_devto_article 走同一个
distribution/devto 适配器——这里不是用户显式触发，而是流程自动化触发；无 DEVTO_API_KEY
时 devto.publish 返回 blocked，LLM 起稿失败也只如实记录：两处失败都不阻塞整轮出报告。
提示词来自 D1 的 promo_article task（/rules/prompts 页面维护），不在代码里硬编码
（test/test_prompts.py 守着）。
"""

from __future__ import annotations

import json

from src.app.config import SITES
from src.app.enums import PromptTask
from src.app.llm.completion import complete_text
from src.app.observability.langsmith import traceable
from src.app.services.prompt_service import prompt_service
from src.app.tools.adapters.distribution import devto
from src.app.utils.json_text import parse_json
from src.app.utils.logger import get_logger
from src.app.utils.text import clip_with_count

MAX_PAYLOAD_CHARS = 6000
ARTICLE_KEYS = ("title", "tags", "target_url", "draft")

log = get_logger(__name__)


def _compact_payload(results: dict) -> str:
    """把取数结果压成 LLM 易读的精简 JSON：只保留 ok 工具的 result，过长截断。"""
    slim: dict = {}
    for name, out in (results or {}).items():
        if isinstance(out, dict) and out.get("ok"):
            slim[name] = out.get("result")
    try:
        text = json.dumps(slim, ensure_ascii=False, default=str)
    except TypeError:
        text = str(slim)
    return clip_with_count(text, MAX_PAYLOAD_CHARS)


def _sites_catalog() -> str:
    """站群一行清单（name=url），给 LLM 选推广目标用。"""
    return "；".join(f"{s.name}={s.url}" for s in SITES.values())


def _resolve_target_url(extracted: dict, proposed: str) -> str:
    """目标链接：LLM 给了合法 http(s) 链接就用它；否则按 target_site 查站群清单。"""
    url = str(proposed or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    site = SITES.get(str(extracted.get("target_site", "")).strip().lower())
    return site.url if site else url


def _article_of(data: dict, extracted: dict) -> dict | None:
    """校验 LLM 产出：title/draft 缺一即视为起稿失败。"""
    if not isinstance(data, dict):
        return None
    article = {k: data.get(k) for k in ARTICLE_KEYS if data.get(k)}
    if not article.get("title") or not article.get("draft"):
        return None
    article["target_url"] = _resolve_target_url(extracted, article.get("target_url", ""))
    article["tags"] = article.get("tags") or ["ai", "tools"]
    return article


class OutreachService:
    """find_backlinks 的起稿 + 发布；失败一律返回 ok:False 的说明 dict，不抛异常。"""

    @traceable(name="draft_article")
    def draft(self, results: dict, extracted: dict,
              versions: dict[str, str] | None = None) -> dict:
        task = PromptTask.PROMO_ARTICLE.value
        versions = versions or {}
        system = prompt_service.load_prompt(task, version=versions.get(task))
        ask = (_compact_payload(results) + "\n\n可推广站点：" + _sites_catalog()
               + "\n只输出 JSON：{\"title\":…, \"tags\":[…], \"target_url\":…, \"draft\":markdown 全文}")
        raw = complete_text(system, ask, warn_event="draft_article_failed")
        article = _article_of(parse_json(raw), extracted)
        if article is None:
            log.warning("外链起稿失败：LLM 未产出可用的文章 JSON")
            return {"ok": False, "status": "draft_failed",
                    "detail": "LLM 未产出可用的文章 JSON（title/draft 缺失）"}
        return {"ok": True, "article": article}

    def run(self, results: dict, extracted: dict,
            versions: dict[str, str] | None = None) -> dict:
        drafted = self.draft(results, extracted, versions)
        if not drafted.get("ok"):
            return drafted
        article = drafted["article"]
        out = devto.publish(article)
        return {"ok": out.get("ok", False), "article": article,
                "status": out.get("status", ""), "detail": out.get("detail", ""),
                "source_url": out.get("source_url", "")}


outreach_service = OutreachService()
