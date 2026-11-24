# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""工具调用入口：两条线都经这里进 registry，契约与打点一致。

两条线都是「名字 → 工具」的表驱动分发，节点侧不写 registry 里的工具名：

    execute_for_intent(意图, ...)  常规线：意图 → 一组取数的 read 工具（INTENT_TOOLS）
    execute_for_step(步骤, **参数)  热词线：逐词步骤 → 该步的那一个工具（STEP_TOOLS）

热词分析（POST /hotwords）与常规线（POST /run）都是「工具编排 + LLM + 落库」，
本身都不是 registry 里的一个工具——差别只在节点类型：热词线逐词回环、收尾走汇总节点。
"""

from __future__ import annotations

from src.app.enums import CurationStatus, HotwordStep, Intent, PageBuildStep, PageQueryMode
from src.app.tools.registry import tool_registry

INTENT_TOOLS: dict[str, tuple[str, ...]] = {
    Intent.WORD_CREATE_PAGE.value: ("get_hotwords", "get_page_inventory", "get_reflections"),
    Intent.PLAN_PROMO.value: ("get_latest_plan", "get_page_inventory", "get_pending_events"),
    Intent.FIND_BACKLINKS.value: ("get_backlink_ledger", "search_reddit", "list_devto_articles"),
    Intent.REVIEW_METRICS.value: ("get_click_stats", "get_search_analytics", "get_page_inventory"),
    Intent.GENERAL.value: ("get_pending_events", "get_reflections"),
}

# 热词线：逐词步骤名 → 工具名。取词与读判例每轮必做（不在可选名单里），
# serp / trend / write_result 是否出现由 graph/hotword_graph.py 的连线名单决定。
STEP_TOOLS: dict[str, str] = {
    HotwordStep.FETCH.value: "next_candidate",
    HotwordStep.EXAMPLES.value: "read_examples",
    HotwordStep.SERP.value: "fetch_serp",
    HotwordStep.TREND.value: "fetch_trend",
    HotwordStep.WRITE_RESULT.value: "write_hotword_result",
}

# 建页线：子图步骤名 → 工具名。pick / classify / build 是纯业务不经 registry（见 PageBuildStep）；
# render / push / verify 三个工具步骤全在这里，节点只说步骤名，不说工具名。
PAGE_STEP_TOOLS: dict[str, str] = {
    PageBuildStep.RENDER.value: "render_files",
    PageBuildStep.PUSH.value: "git_push",
    PageBuildStep.VERIFY.value: "check_live_page",
}

TOOL_ARGS = {
    # ★word_create_page 读「待开发」池：承接热词分析的产出（分析判「做」→ saved/todo），
    #   不再看原始未跟进词——漏斗顺序 = 分析筛选 → 排产 → 建页。
    "get_hotwords": lambda t, e: {"limit": 20, "status": CurationStatus.TODO.value},
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

    def _run(self, name: str, **kwargs):
        """按 registry 工具名调一个工具，返回统一 ToolOutput dict。

        私有：两条线的调用方都只能通过名字表分发进来（execute_for_intent /
        execute_for_step），不允许把工具名字面量写回节点。
        """
        return self.registry.run(name, **kwargs)

    def execute_for_step(self, step: str, **kwargs) -> dict:
        """热词线：逐词步骤名 → 该步的工具，参数由节点按这一步的需要给出。"""
        name = STEP_TOOLS.get(step)
        if name is None or name not in self.registry.names():
            return {"ok": False, "error": f"unknown step: {step}", "retryable": False}
        return self._run(name, **kwargs)

    def execute_for_page(self, step: str, **kwargs) -> dict:
        """建页线：子图步骤名 → 该步的工具（render / push / verify），与热词线同构。"""
        name = PAGE_STEP_TOOLS.get(step)
        if name is None or name not in self.registry.names():
            return {"ok": False, "error": f"unknown step: {step}", "retryable": False}
        return self._run(name, **kwargs)

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
            results[name] = self._run(name, **kwargs)
        return results


tool_service = ToolService()
