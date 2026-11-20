# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""工具调用入口：两条线都经这里进 registry，契约与打点一致。

常规线按意图选一组工具采集事实（execute_for_intent，默认集只含 read 工具）；
热词线按名逐个调（run）：取词 / 读判例 / 补 SERP 与趋势 / 回写结论。

热词分析（POST /hotwords）与常规线（POST /run）都是「工具编排 + LLM + 落库」，
本身都不是 registry 里的一个工具——差别只在节点类型：热词线逐词回环、收尾走汇总节点。
"""

from __future__ import annotations

from src.app.enums import Intent, PageQueryMode
from src.app.tools.registry import tool_registry

INTENT_TOOLS: dict[str, tuple[str, ...]] = {
    Intent.SELECT_PAGES.value: ("get_hotwords", "get_page_inventory", "get_reflections"),
    Intent.PLAN_PROMO.value: ("get_latest_plan", "get_page_inventory", "get_pending_events"),
    Intent.FIND_BACKLINKS.value: ("get_backlink_ledger", "search_reddit", "list_devto_articles"),
    Intent.REVIEW_METRICS.value: ("get_click_stats", "get_search_analytics", "get_page_inventory"),
    Intent.GENERAL.value: ("get_pending_events", "get_reflections"),
}

TOOL_ARGS = {
    "get_hotwords": lambda t, e: {"limit": 20},
    "get_page_inventory": lambda t, e: {"mode": PageQueryMode.NEW.value, "limit": 10},
    "get_reflections": lambda t, e: {"limit": 5},
    "get_pending_events": lambda t, e: {"limit": 20},
    "get_latest_plan": lambda t, e: {},
    "get_backlink_ledger": lambda t, e: {"limit": 20},
    "search_reddit": lambda t, e: {"query": (t or "AI tools")[:80], "limit": 5},
    "list_devto_articles": lambda t, e: {"tags": ["ai"]},
    "get_click_stats": lambda t, e: {"days": 7},
    "get_search_analytics": lambda t, e: {"days": 7},
}


class ToolService:
    def __init__(self) -> None:
        # 复用 tools/registry.py 的那个单例，避免工具被实例化两遍
        self.registry = tool_registry

    def run(self, name: str, **kwargs):
        """按名调一个工具，返回统一 ToolOutput dict（热词线的逐词 IO 走这里）。"""
        return self.registry.run(name, **kwargs)

    def execute_for_intent(self, intent: str, text: str,
                           extracted: dict, tools: list[str] | None = None,
                           prompt_versions: dict[str, str] | None = None) -> dict:
        names = list(tools) if tools else list(
            INTENT_TOOLS.get(intent, INTENT_TOOLS[Intent.GENERAL.value]))
        results: dict = {}
        for name in names:
            if name not in self.registry.names():
                results[name] = {"tool": name, "ok": False,
                                "error": "unknown tool", "retryable": False}
                continue
            builder = TOOL_ARGS.get(name)
            kwargs = builder(text, extracted) if builder else {}
            results[name] = self.run(name, **kwargs)
        return results


tool_service = ToolService()
