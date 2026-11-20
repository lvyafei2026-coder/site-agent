# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""后台线（热词逐词循环图）的装配：只认识 nodes 的循环节点，不拉主图依赖链。

2026-11-20 分层归位：节点逻辑在 nodes/hotword_nodes.py（本层单向依赖 nodes），
批次驱动在 graph/hotword_batch.py（invoke 本图），单词分析与回填在 services/，
判据与 IO 在 tools/adapters/terms/ 叶子层。依赖全程单向向下，无环、无函数内 import。

两图装配并列在 graph/：user_graph.py（用户线主图）与本模块（后台线循环图），
graph/__init__.py 统一导出，入口一眼可见两条线。
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from src.app.nodes import hotword_nodes as _nodes


def build_hotword_graph():
    """后台线：fetch_one → analyze_one → should_continue 回环。

    读一个热词 → 分析 → 回环，直到词用完或达到 limit（最大循环数）。循环完全由
    条件边驱动，Python 层不写 while/for。节点逻辑在 nodes/hotword_nodes.py，本函数只装配结构。
    不传 checkpointer：单次 invoke 跑完即弃，不跨请求续跑。
    """
    graph = StateGraph(_nodes.HotwordLoopState)
    graph.add_node(_nodes.FETCH_NODE, _nodes.fetch_one)
    graph.add_node(_nodes.ANALYZE_NODE, _nodes.analyze_one)
    graph.add_edge(START, _nodes.FETCH_NODE)
    graph.add_edge(_nodes.FETCH_NODE, _nodes.ANALYZE_NODE)
    graph.add_conditional_edges(
        _nodes.ANALYZE_NODE,
        _nodes.should_continue,
        {_nodes.FETCH_NODE: _nodes.FETCH_NODE, END: END},
    )
    return graph.compile()


@lru_cache(maxsize=1)
def get_hotword_graph():
    """后台线图进程级单例（图无状态，运行时数据全在 invoke 的 state 里）。"""
    return build_hotword_graph()
