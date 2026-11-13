from __future__ import annotations

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


def build_graph():
    """单轮流水线：classify_intent → run_processing → validate_result → output / retry / error。"""
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
    """进程级单例。checkpointer 持有连接（sqlite 尤甚），不能每个请求重建一次。"""
    return build_graph()
