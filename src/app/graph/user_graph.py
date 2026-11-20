# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""用户线主图的装配（对外用户输入）。

2026-11-20 分层归位：原 builder.py 拆成两张对称命名的装配文件——
    user_graph.py     用户线主图（本文件）：classify_intent → run_processing → … → output
    hotword_graph.py  热词线循环图：fetch_one → analyze_one → should_continue 回环
两图并列、互不嵌套，由 graph/__init__.py 统一导出；两条线各有独立入口
（用户线 `POST /run`，热词线 `POST /hotwords`），按需调用，进程内无常驻循环。

本文件只装配结构，节点逻辑在 nodes/，依赖单向向下（graph → nodes → services → tools → adapters）。
"""

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


def build_user_graph():
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
def get_user_graph():
    """用户线图进程级单例。checkpointer 持有连接（sqlite 尤甚），不能每请求重建。"""
    return build_user_graph()
