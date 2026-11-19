from __future__ import annotations

"""图装配层：两条线在这里并列定义，入口（main.py）一眼可见。

线 1｜对外用户输入（用户线）
    build_graph() → get_graph()
    classify_intent → run_processing → validate_result → build_output / retry / error
    驱动方：HTTP /run（api/routes.py）、CLI（main.py:run）。带 checkpointer，按 thread_id 续跑。

线 2｜后台自动运行（后台线）
    build_hotword_graph() → get_hotword_graph()
    fetch_one → analyze_one → should_continue 回环（读一词→分析→回环，直到词用完或达 limit）
    驱动方：main.py:run_background()。不传 checkpointer，单次 invoke 跑完即弃。

两图彼此独立、互不嵌套：主图不知道热词循环的存在，热词循环也不是请求级流程。
共享的只是底层节点/工具/适配器实现，图结构各管各的。节点逻辑不动——本文件只装配结构。
"""

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from src.app.graph.constants import Names
from src.app.graph.edges import (
    EDGE_MAP,
    PROCESSING_EDGE_MAP,
    route_after_processing,
    route_after_validation,
)
from src.app.graph.state import WorkflowState
from src.app.memory.checkpointer import get_checkpointer
from src.app.nodes.error import error_node
from src.app.nodes.intent import intent_node
from src.app.nodes.output import output_node
from src.app.nodes.processing import processing_node
from src.app.nodes.validation import validation_node
# 后台线的节点逻辑：真身在 adapters/terms/hotword_loop.py，这里只 import 来装配结构。
from src.app.tools.adapters.terms import hotword_loop as _hwloop


# ══ 线 1：对外用户输入 ═══════════════════════════════════════════════
def build_graph():
    """用户线：classify_intent → run_processing → validate_result → output / retry / error。

    由 HTTP /run 与 CLI 驱动；带 checkpointer，可按 thread_id 续跑、中断恢复。
    """
    graph = StateGraph(WorkflowState)

    graph.add_node(Names.INTENT, intent_node)
    graph.add_node(Names.PROCESSING, processing_node)
    graph.add_node(Names.VALIDATION, validation_node)
    graph.add_node(Names.OUTPUT, output_node)
    graph.add_node(Names.ERROR, error_node)

    graph.add_edge(START, Names.INTENT)
    graph.add_edge(Names.INTENT, Names.PROCESSING)
    # PROCESSING → VALIDATION 不是无条件边：processing 硬失败（LLM 不可用 / 工具抛异常）
    # 直接短路到 ERROR，不再空转重试。软失败仍交给 validation 决定重试。
    graph.add_conditional_edges(Names.PROCESSING, route_after_processing, PROCESSING_EDGE_MAP)
    graph.add_conditional_edges(Names.VALIDATION, route_after_validation, EDGE_MAP)
    graph.add_edge(Names.OUTPUT, END)
    graph.add_edge(Names.ERROR, END)

    return graph.compile(checkpointer=get_checkpointer())


@lru_cache(maxsize=1)
def get_graph():
    """用户线图进程级单例。checkpointer 持有连接（sqlite 尤甚），不能每请求重建。"""
    return build_graph()


# ══ 线 2：后台自动运行 ═══════════════════════════════════════════════
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
