# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""工具分发：按意图选一组工具采集事实。

默认集只含 read 工具。热词分析（analyze_hotwords）是编排而非工具，由独立的
`POST /hotwords` 走 graph 的热词图，不经过本分发器。
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
            results[name] = self.registry.run(name, **kwargs)
        return results


tool_service = ToolService()
