# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词创页排产（word_plan_page 专属的共用能力）：待开发词 → 本轮排产方案。

由 processing_service 在取数成功后调用；三份输入各管一个决策维度：
    get_hotwords(todo)    做什么——待开发池
    get_page_inventory    什么已经做了——近 30 天新页（查重 / 防撞车）
    get_reflections       上次怎么翻车的——历史复盘（避坑）
返回 markdown 排产方案正文（选词 + 落站 + 排除原因）。无 key 或调用失败返回
空串，上层跳过该段，不阻塞整轮。提示词来自 D1 的 word_plan_page task。
"""

from __future__ import annotations

import json

from src.app.config import SITES
from src.app.enums import PromptTask
from src.app.llm.completion import complete_text
from src.app.observability.langsmith import traceable
from src.app.services.prompt_service import prompt_service
from src.app.utils.text import clip_with_count

MAX_PAYLOAD_CHARS = 8000


def _sites_line() -> str:
    """站群清单（name + brand）随排产 payload 一起给 LLM。

    落站匹配不能只依赖 get_page_inventory 的已建页风格——新站群冷启动时那张表
    是空的，没有这份清单 LLM 只能给「待定」。brand / repo 名自带语义
    （ai-background-remover 之类），足以按词义直接落站，不捏造。
    """
    return [{"name": s.name, "brand": s.brand} for s in SITES.values()]


def _compact_payload(results: dict) -> str:
    """把三个工具的结果压成 LLM 易读的精简 JSON：只取 ok 工具的 result，过长截断。"""
    slim: dict = {}
    for name, out in (results or {}).items():
        if isinstance(out, dict) and out.get("ok"):
            slim[name] = out.get("result")
    slim["sites"] = _sites_line()
    try:
        text = json.dumps(slim, ensure_ascii=False, default=str)
    except TypeError:
        text = str(slim)
    return clip_with_count(text, MAX_PAYLOAD_CHARS)


class PlanningService:
    @traceable(name="plan_pages")
    def plan(self, results: dict, versions: dict[str, str] | None = None) -> str:
        versions = versions or {}
        system = prompt_service.load_prompt(
            PromptTask.WORD_PLAN_PAGE.value,
            version=versions.get(PromptTask.WORD_PLAN_PAGE.value))
        return complete_text(system, _compact_payload(results),
                             warn_event="plan_pages_failed")


planning_service = PlanningService()
