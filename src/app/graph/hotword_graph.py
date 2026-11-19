# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""后台线（热词逐词循环图）的装配：只认识 hotword_loop，不拉主图依赖链。

为什么单独成模块（而不是放在 graph/builder.py）：
    builder 顶层 import nodes → services → tool_service → registry → hotwords_tools
    → hotwords 门面 → hotword_batch。若 batch 顶层再 import builder 取图，就成加载期环。
    本模块只 import hotword_loop（它一路向下到 worker/fetch/criteria/llm/kv_io/contract，
    都不回指 graph 或 batch），所以 batch 可以安全地顶层 import 这里。

builder.py 仍是「两张图的统一装配门面」：它 re-export 本模块的 build/get 函数，
入口（main.py / api）照旧从 builder 同时看到两条线。本文件只是把后台图的装配下沉成叶子，
让依赖恢复单向：batch → hotword_graph → loop → worker → …，全程静态 import，无惰性边界。
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from src.app.tools.adapters.terms import hotword_loop as _hwloop


def build_hotword_graph():
    """后台线：fetch_one → analyze_one → should_continue 回环。

    读一个热词 → 分析 → 回环，直到词用完或达到 limit（最大循环数）。循环完全由
    条件边驱动，Python 层不写 while/for。节点逻辑在 hotword_loop.py，本函数只装配结构。
    不传 checkpointer：单次 invoke 跑完即弃，不跨请求续跑。
    """
    graph = StateGraph(_hwloop.HotwordLoopState)
    graph.add_node(_hwloop.FETCH_NODE, _hwloop.fetch_one)
    graph.add_node(_hwloop.ANALYZE_NODE, _hwloop.analyze_one)
    graph.add_edge(START, _hwloop.FETCH_NODE)
    graph.add_edge(_hwloop.FETCH_NODE, _hwloop.ANALYZE_NODE)
    graph.add_conditional_edges(
        _hwloop.ANALYZE_NODE,
        _hwloop.should_continue,
        {_hwloop.FETCH_NODE: _hwloop.FETCH_NODE, END: END},
    )
    return graph.compile()


@lru_cache(maxsize=1)
def get_hotword_graph():
    """后台线图进程级单例（图无状态，运行时数据全在 invoke 的 state 里）。"""
    return build_hotword_graph()
