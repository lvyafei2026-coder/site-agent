# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""工具分发：按意图选一组工具采集事实。

默认集只含 read 工具——唯一例外是 analyze_hotwords：这个意图的语义就是
「批量分析并回写候选状态」，所以它默认带一个 write 工具。其余 ledger / write
工具仍只在请求显式传 tools 时触发。
"""

from __future__ import annotations

from src.app.tools.registry import ToolRegistry, tool_registry

INTENT_TOOLS: dict[str, tuple[str, ...]] = {
    "select_pages": ("get_hotwords", "get_page_inventory", "get_reflections"),
    "plan_promo": ("get_latest_plan", "get_page_inventory", "get_pending_events"),
    "find_backlinks": ("get_backlink_ledger", "search_reddit", "list_devto_articles"),
    "review_metrics": ("get_click_stats", "get_search_analytics", "get_page_inventory"),
    "analyze_hotwords": ("analyze_hotwords",),
    "general": ("get_pending_events", "get_reflections"),
}

TOOL_ARGS = {
    "get_hotwords": lambda t, e: {"limit": 20},
    "get_page_inventory": lambda t, e: {"mode": "new", "limit": 10},
    "get_reflections": lambda t, e: {"limit": 5},
    "get_pending_events": lambda t, e: {"limit": 20},
    "get_latest_plan": lambda t, e: {},
    "get_backlink_ledger": lambda t, e: {"limit": 20},
    "search_reddit": lambda t, e: {"query": (t or "AI tools")[:80], "limit": 5},
    "list_devto_articles": lambda t, e: {"tags": ["ai"]},
    "get_click_stats": lambda t, e: {"days": 7},
    "get_search_analytics": lambda t, e: {"days": 7},
    "analyze_hotwords": lambda t, e: {"limit": 20},
}


class ToolService:
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        # 复用 tools/registry.py 的那个单例，避免 26 个工具被实例化两遍
        self.registry = registry or tool_registry

    def execute_for_intent(self, intent: str, text: str,
                           extracted: dict, tools: list[str] | None = None,
                           prompt_versions: dict[str, str] | None = None) -> dict:
        names = list(tools) if tools else list(INTENT_TOOLS.get(intent, INTENT_TOOLS["general"]))
        results: dict = {}
        for name in names:
            if name not in self.registry.names():
                results[name] = {"tool": name, "ok": False,
                                "error": "unknown tool", "retryable": False}
                continue
            builder = TOOL_ARGS.get(name)
            kwargs = builder(text, extracted) if builder else {}
            # 热词分析是唯一自己调 LLM 的工具，把 /run 里的 prompt_versions.hotwords 透传下去，
            # 让「换提示词 = 新增 vN.txt + 请求里带版本」在整条链路上都成立。
            if name == "analyze_hotwords" and prompt_versions:
                version = prompt_versions.get("hotwords")
                if version:
                    kwargs["prompt_version"] = version
            results[name] = self.registry.run(name, **kwargs)
        return results


tool_service = ToolService()
