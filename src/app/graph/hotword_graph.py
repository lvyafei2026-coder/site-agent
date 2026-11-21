# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词循环图：装配 + 运行入口（与 normal_graph.py 同构），步骤编排的真源也在这里。

外层逐词回环，内层是**按本轮名单编译的逐词子图**——「某个步骤这轮跑不跑」= 图上连不连
这个节点，而不是 Python 里的 if/for：

    START → fetch_one → route_after_fetch ─┬─ analyze_word → route_after_word ─┬─ fetch_one（还有词）
                                          └─ build_summary → END             └─ build_summary

    analyze_word（逐词子图）：START → gather_serp → gather_trend → llm_analyze
                             → route_after_llm ─┬─ write_result → collect_word → END
                                                └─ collect_word → END（没结论就不写 KV）

名单 → 节点的对应表只有下面两张：加一个证据源 = tools/hotwords_tools.py 加一个工具
+ nodes/hotword_xxx_node.py 加一个节点 + GraphNode 加一个值 + 这里加一行；
循环、请求模型、汇总节点都不用改。

run_hotwords() 与常规线 run_workflow() 是**同一套机制**：
    initial_hotword_state() → run_traced(图, state, config, snapshot=hotword_snapshot)
    → 终态 + trace + langsmith（回链 traceRun 由 @observed 切面注入）
差别只在节点类型与 state 内容：这条线是逐词回环、收尾走 build_summary 节点，
且 state 带本轮步骤名单与降级状态，所以对外返回终态的 `summary` 而非整份 state。

节点逻辑在 nodes/hotword_*_node.py（一节点一文件）；取词 / 补证据 / 后置动作与常规线
一样经 tool_service 调 registry 里的工具（实现在 tools/hotwords_tools.py +
tools/adapters/hotwords/ 叶子）。依赖全程单向向下，无环、无函数内 import。
"""

from __future__ import annotations

import time
import uuid
from functools import lru_cache
from typing import Any, Callable, Optional

from langgraph.graph import END, START, StateGraph

from src.app.config import settings
from src.app.enums import GraphNode, RouteTag, TraceName
from src.app.graph.edges import (
    FETCH_EDGE_MAP, WORD_EDGE_MAP, route_after_fetch, route_after_llm, route_after_word,
)
from src.app.graph.state import HotwordLoopState, initial_hotword_state
from src.app.nodes.hotword_collect_node import collect_word
from src.app.nodes.hotword_fetch_node import fetch_one
from src.app.nodes.hotword_llm_node import llm_analyze
from src.app.nodes.hotword_serp_node import gather_serp
from src.app.nodes.hotword_summary_node import summary_node
from src.app.nodes.hotword_trend_node import gather_trend
from src.app.nodes.hotword_write_node import write_result
from src.app.observability.langsmith import observed, public_config
from src.app.observability.trace import hotword_snapshot, run_traced

StepNode = tuple[GraphNode, Callable[[HotwordLoopState], dict[str, Any]]]

# LLM 之前的补证据步骤：名单里出现这个名字 = 逐词子图里连了这个节点。
EVIDENCE_STEPS: dict[str, StepNode] = {
    "serp": (GraphNode.GATHER_SERP, gather_serp),     # fetch_serp：结果摘要进提示词
    "trend": (GraphNode.GATHER_TREND, gather_trend),  # fetch_trend：只落库，不进提示词
}
# LLM 之后的后置动作步骤。
POST_STEPS: dict[str, StepNode] = {
    "write_result": (GraphNode.WRITE_RESULT, write_result),  # write_hotword_result
}

DEFAULT_EVIDENCE = tuple(EVIDENCE_STEPS)
DEFAULT_POST = tuple(POST_STEPS)


def _resolve(names: Optional[list[str]], table: dict[str, StepNode],
             default: tuple[str, ...], what: str) -> tuple[str, ...]:
    picked = list(default) if names is None else list(names)
    for name in picked:
        if name not in table:
            raise ValueError(f"未知{what}步骤：{name}（可选：{'、'.join(table)}）")
    return tuple(picked)


def resolve_steps(evidence: Optional[list[str]] = None,
                  post: Optional[list[str]] = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """名单 → 校验过的步骤名元组；None 用默认计划，空列表 = 那一段一个节点都不连。

    ★白名单是上面两张表，不是任意 registry 工具名——/hotwords 这个入口不带强鉴权，
      绝不能让它变成 git_push / send_notice 之类不可逆动作的旁路。未知名字在这里就抛
      ValueError，由 run_hotwords 收进 state.load_error，走与 KV 不可达同一条失败收口
      路径（一次 IO 都不发）。
    """
    return (_resolve(evidence, EVIDENCE_STEPS, DEFAULT_EVIDENCE, "证据"),
            _resolve(post, POST_STEPS, DEFAULT_POST, "后置动作"))


def build_word_graph(evidence: tuple[str, ...], post: tuple[str, ...]):
    """按名单串出逐词子图：补证据 → LLM → 后置动作 → 落账。

    「这一步本轮不做」= 图上没有这个节点，所以节点内部只管自己的源级降级，不再读名单。
    所有分支（含 LLM 失败）都汇到 collect_word，失败只落在那一个词身上。
    """
    graph = StateGraph(HotwordLoopState)
    gathers = [EVIDENCE_STEPS[name] for name in evidence]
    actions = [POST_STEPS[name] for name in post]

    for node, fn in [*gathers, (GraphNode.LLM_ANALYZE, llm_analyze),
                     (GraphNode.COLLECT_WORD, collect_word), *actions]:
        graph.add_node(node, fn)

    chain = [node for node, _ in gathers]
    graph.add_edge(START, chain[0] if chain else GraphNode.LLM_ANALYZE)
    for src, dst in zip(chain, [*chain[1:], GraphNode.LLM_ANALYZE]):
        graph.add_edge(src, dst)

    if actions:
        tail = [node for node, _ in actions]
        graph.add_conditional_edges(
            GraphNode.LLM_ANALYZE, route_after_llm,
            {RouteTag.NEXT: tail[0], RouteTag.COLLECT: GraphNode.COLLECT_WORD})
        for src, dst in zip(tail, [*tail[1:], GraphNode.COLLECT_WORD]):
            graph.add_edge(src, dst)
    else:
        graph.add_edge(GraphNode.LLM_ANALYZE, GraphNode.COLLECT_WORD)

    graph.add_edge(GraphNode.COLLECT_WORD, END)
    return graph.compile()


def build_hotword_graph(evidence: tuple[str, ...] = DEFAULT_EVIDENCE,
                        post: tuple[str, ...] = DEFAULT_POST):
    """装配逐词循环图：取一词 → 跑该词的逐词子图 → 条件边决定回环还是进汇总节点。

    循环完全由条件边驱动，Python 层不写 while/for。不传 checkpointer：
    一次跑完即弃，不跨请求续跑（常规线要按 thread_id 续跑，故那边带 checkpointer）。
    逐词子图整体只占外层一个 super-step，所以外层的 recursion_limit 仍只按词数算。
    """
    graph = StateGraph(HotwordLoopState)

    graph.add_node(GraphNode.FETCH_ONE, fetch_one)
    graph.add_node(GraphNode.ANALYZE_WORD, build_word_graph(evidence, post))
    graph.add_node(GraphNode.SUMMARY, summary_node)

    graph.add_edge(START, GraphNode.FETCH_ONE)
    graph.add_conditional_edges(GraphNode.FETCH_ONE, route_after_fetch, FETCH_EDGE_MAP)
    graph.add_conditional_edges(GraphNode.ANALYZE_WORD, route_after_word, WORD_EDGE_MAP)
    graph.add_edge(GraphNode.SUMMARY, END)

    return graph.compile()


@lru_cache(maxsize=8)
def get_hotword_graph(evidence: tuple[str, ...] = DEFAULT_EVIDENCE,
                      post: tuple[str, ...] = DEFAULT_POST):
    """按计划缓存编译好的图（名单是元组，可直接当键；图无状态，数据全在每轮的 state 里）。"""
    return build_hotword_graph(evidence, post)


def _run_inputs(limit: int = 20, prompt_version: Optional[str] = None,
                evidence: Optional[list[str]] = None,
                post: Optional[list[str]] = None) -> dict[str, Any]:
    """根 span 的 inputs：本轮实际生效的步骤名单（None = 默认计划）。"""
    return {
        "limit": limit,
        "promptVersion": prompt_version
        or (settings.default_prompt_versions or {}).get("hotwords", "v1"),
        "evidence": list(DEFAULT_EVIDENCE if evidence is None else evidence),
        "post": list(DEFAULT_POST if post is None else post),
    }


def _run_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """根 span 的 outputs：LangSmith 上直接看成功数与降级情况，不用点开 items。"""
    keys = ("ok", "fetched", "analyzed", "failed", "successRate", "counts",
            "evidenceSteps", "disabledSources", "llmFailures", "error", "note")
    return {k: out[k] for k in keys if k in out}


@observed(TraceName.HOTWORD_RUN, kind="chain", inputs=_run_inputs,
          outputs=_run_outputs, tags=["site-agent", "hotword-run"], link="traceRun")
def run_hotwords(limit: int = 20,
                 prompt_version: Optional[str] = None,
                 evidence: Optional[list[str]] = None,
                 post: Optional[list[str]] = None) -> dict[str, Any]:
    """跑一轮热词分析：问 tool 逐词取 → 逐词跑子图 → 没有更多词或达上限就汇总收尾。

    limit 是最大循环次数（1-50，由 state 工厂收口）。evidence / post 是步骤名单，
    只能取本文件两张表里的名字（None=默认计划，[]=该段不做）；名单非法或
    KV 候选池不可达时整轮失败，不写任何状态。
    每个词独立跑完、失败只落在该词 items[].error；源级故障只降级「取数」那一个节点。
    trace 由切面与共用驱动器承担，函数体只有纯业务：解析名单 → 构造 state → 跑图 → 取终态。
    """
    run_id = f"hotwords-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    try:
        plan = resolve_steps(evidence, post)
        load_error = ""
    except ValueError as exc:
        # 名单非法 = 开跑前就该失败：仍按默认计划取那张缓存好的图，一次 IO 都不发地进收尾节点
        plan = (DEFAULT_EVIDENCE, DEFAULT_POST)
        load_error = str(exc)

    initial = initial_hotword_state(limit, prompt_version, *plan)
    if load_error:
        initial["load_error"] = load_error

    # 每词 2 个 super-step（fetch_one + analyze_word），取尽那轮多 1 次 fetch，收尾再加汇总节点；
    # limit 最大 50 → 最坏 2*50+3 = 103。给足上限，让 route_after_word 决定何时收尾。
    final, trace = run_traced(
        get_hotword_graph(*plan),
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
