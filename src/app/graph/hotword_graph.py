# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词循环图：装配 + 运行入口（与 normal_graph.py 同构）。

图结构（fetch_one 每轮问 tool 要一个词，处理完再问「还有吗」）：
    START → fetch_one → analyze_one → route_after_analyze ─┬─ fetch_one（还有）
                                                          └─ build_summary → END

run_hotwords() 与常规线 run_workflow() 是**同一套机制**：
    initial_hotword_state() → run_traced(图, state, config, snapshot=hotword_snapshot)
    → 终态 + trace + langsmith（回链 traceRun 由 @observed 切面注入）
差别只在节点类型与 state 内容：这条线是逐词回环、收尾走 build_summary 节点，
且 state 带 kv / degrade 运行时句柄，所以对外返回终态的 `summary` 而非整份 state。

节点逻辑在 nodes/hotword_*_node.py（一节点一文件），单词编排在 services/hotword_service.py；
取词 / 补数据 / 回写与常规线一样经 tool_service 调 registry 里的工具（实现在
tools/hotwords_tools.py + tools/adapters/hotwords/ 叶子）。依赖全程单向向下，无环、无函数内 import。
"""

from __future__ import annotations

import time
import uuid
from functools import lru_cache
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from src.app.config import settings
from src.app.enums import GraphNode, TraceName
from src.app.graph.edges import HOTWORD_EDGE_MAP, route_after_analyze
from src.app.graph.state import HotwordLoopState, initial_hotword_state
from src.app.nodes.hotword_analyze_node import analyze_one
from src.app.nodes.hotword_fetch_node import fetch_one
from src.app.nodes.hotword_summary_node import summary_node
from src.app.observability.langsmith import observed, public_config
from src.app.observability.trace import hotword_snapshot, run_traced


def build_hotword_graph():
    """装配逐词循环图：取一词 → 分析 → 条件边决定回环还是进汇总节点。

    循环完全由条件边驱动，Python 层不写 while/for。不传 checkpointer：
    一次跑完即弃，不跨请求续跑（常规线要按 thread_id 续跑，故那边带 checkpointer）。
    """
    graph = StateGraph(HotwordLoopState)

    graph.add_node(GraphNode.FETCH_ONE, fetch_one)
    graph.add_node(GraphNode.ANALYZE_ONE, analyze_one)
    graph.add_node(GraphNode.SUMMARY, summary_node)

    graph.add_edge(START, GraphNode.FETCH_ONE)
    graph.add_edge(GraphNode.FETCH_ONE, GraphNode.ANALYZE_ONE)
    graph.add_conditional_edges(GraphNode.ANALYZE_ONE, route_after_analyze, HOTWORD_EDGE_MAP)
    graph.add_edge(GraphNode.SUMMARY, END)

    return graph.compile()


@lru_cache(maxsize=1)
def get_hotword_graph():
    """热词图进程级单例（图无状态，运行时数据全在每轮的 state 里）。"""
    return build_hotword_graph()


def _run_inputs(limit: int = 20, prompt_version: Optional[str] = None) -> dict[str, Any]:
    return {
        "limit": limit,
        "promptVersion": prompt_version
        or (settings.default_prompt_versions or {}).get("hotwords", "v1"),
    }


def _run_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """根 span 的 outputs：LangSmith 上直接看成功数与降级情况，不用点开 items。"""
    keys = ("ok", "fetched", "analyzed", "failed", "successRate", "counts",
            "serpDisabled", "trendsDisabled", "llmFailures", "error", "note")
    return {k: out[k] for k in keys if k in out}


@observed(TraceName.HOTWORD_RUN, kind="chain", inputs=_run_inputs,
          outputs=_run_outputs, tags=["site-agent", "hotword-run"], link="traceRun")
def run_hotwords(limit: int = 20,
                 prompt_version: Optional[str] = None) -> dict[str, Any]:
    """跑一轮热词分析：问 tool 逐词取 → 逐词分析 → 没有更多词或达上限就汇总收尾。

    limit 是最大循环次数（1-50，由 state 工厂收口）。KV 候选池不可达时整轮失败，不写任何状态。
    每个词独立跑完、失败只落在该词 items[].error；源级故障只降级「取数」这一步。
    trace 由切面与共用驱动器承担，函数体只有纯业务：构造 state → 跑图 → 取终态。
    """
    run_id = f"hotwords-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    initial = initial_hotword_state(limit, prompt_version)
    # 每词 2 个 super-step（fetch + analyze），取尽那轮多 1 次 fetch，收尾再加汇总节点；
    # limit 最大 50 → 最坏 2*50+3 = 103。给足上限，让 route_after_analyze 决定何时收尾。
    final, trace = run_traced(
        get_hotword_graph(),
        initial,
        config={
            "recursion_limit": initial["limit"] * 2 + 4,
            "tags": ["site-agent"],
            "metadata": {"runId": run_id, "limit": initial["limit"]},
        },
        snapshot=hotword_snapshot,
    )
    return {**final["summary"], "runId": run_id, "trace": trace,
            "langsmith": public_config()}
