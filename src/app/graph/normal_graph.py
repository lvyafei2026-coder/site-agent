# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""常规线主图：装配 + 运行入口（与 hotword_graph.py 同构）。

    normal_graph.py   常规线主图（本文件）：classify_intent → run_processing → … → output
    hotword_graph.py  热词线循环图：fetch_one → analyze_word（逐词子图）→ 回环 / build_summary

两图并列、互不嵌套，由 graph/__init__.py 统一导出；两条线各有独立 HTTP 入口
（`POST /run` / `POST /hotwords`），但**构造与触发机制完全一致**：
    build_*_graph() → get_*_graph() 单例 → initial_*_state() → run_traced(...)
    → 终态 + trace + langsmith（+ 切面注入的 traceRun 回链）
差别只在节点类型、state 内容与是否带 checkpointer（本线要按 thread_id 续跑）。

本文件只装配结构与驱动一轮，节点逻辑在 nodes/，依赖单向向下
（graph → nodes → services → tools → adapters）。
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from src.app.enums import GraphNode, TraceName
from src.app.graph.edges import (
    EDGE_MAP,
    PROCESSING_EDGE_MAP,
    route_after_processing,
    route_after_validation,
)
from src.app.graph.state import WorkflowState, initial_workflow_state
from src.app.memory.checkpointer import get_checkpointer
from src.app.nodes.normal_error_node import error_node
from src.app.nodes.normal_intent_node import intent_node
from src.app.nodes.normal_output_node import output_node
from src.app.nodes.normal_processing_node import processing_node
from src.app.nodes.normal_validation_node import validation_node
from src.app.observability.langsmith import observed, public_config
from src.app.observability.trace import run_traced, workflow_snapshot


def build_normal_graph():
    """常规线：classify_intent → run_processing → validate_result → output / retry / error。

    由 run_workflow() 驱动；带 checkpointer，可按 thread_id 续跑、中断恢复。
    """
    graph = StateGraph(WorkflowState)

    graph.add_node(GraphNode.INTENT, intent_node)
    graph.add_node(GraphNode.PROCESSING, processing_node)
    graph.add_node(GraphNode.VALIDATION, validation_node)
    graph.add_node(GraphNode.OUTPUT, output_node)
    graph.add_node(GraphNode.ERROR, error_node)

    graph.add_edge(START, GraphNode.INTENT)
    graph.add_edge(GraphNode.INTENT, GraphNode.PROCESSING)
    # PROCESSING → VALIDATION 不是无条件边：processing 硬失败（LLM 不可用 / 工具抛异常）
    # 直接短路到 ERROR，不再空转重试。软失败仍交给 validation 决定重试。
    graph.add_conditional_edges(GraphNode.PROCESSING, route_after_processing, PROCESSING_EDGE_MAP)
    graph.add_conditional_edges(GraphNode.VALIDATION, route_after_validation, EDGE_MAP)
    graph.add_edge(GraphNode.OUTPUT, END)
    graph.add_edge(GraphNode.ERROR, END)

    return graph.compile(checkpointer=get_checkpointer())


@lru_cache(maxsize=1)
def get_normal_graph():
    """常规线图进程级单例。checkpointer 持有连接（sqlite 尤甚），不能每请求重建。"""
    return build_normal_graph()


def _run_inputs(input_text: str, intent: Optional[str] = None) -> dict[str, Any]:
    return {"input_text": input_text[:200], "intent": intent or "auto"}


def _run_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """根 span 的 outputs：意图 / 置信度 / 报告 / 错误，不塞整份 tool_results。"""
    keys = ("intent", "confidence", "output", "error")
    return {k: out[k] for k in keys if k in out}


@observed(TraceName.WORKFLOW_RUN, kind="chain", inputs=_run_inputs,
          outputs=_run_outputs, tags=["site-agent"], link="traceRun")
def run_workflow(input_text: str, *, intent: Optional[str] = None,
                 tools: Optional[list[str]] = None,
                 tool_args: Optional[dict[str, dict]] = None,
                 prompt_versions: Optional[dict[str, str]] = None,
                 thread_id: Optional[str] = None) -> dict[str, Any]:
    """跑一轮常规线主图（与热词线 run_hotwords 同一套机制）。

    initial_workflow_state() → run_traced(图, state, config, snapshot=workflow_snapshot)
    → 终态 + trace + langsmith（回链 traceRun 由切面注入）。
    thread_id 决定 checkpointer 的会话归属，可按同一 thread 续跑、中断恢复。
    """
    state = initial_workflow_state(input_text, intent=intent, tools=tools,
                                   tool_args=tool_args,
                                   prompt_versions=prompt_versions)
    thread_id = thread_id or f"run-{int(time.time() * 1000)}"
    final, trace = run_traced(
        get_normal_graph(),
        state,
        config={
            "configurable": {"thread_id": thread_id},
            # run_name / tags / metadata 只在 LangSmith 开启时才有意义，关闭时被忽略：
            # 有了 run_name，追踪列表里图这一层是 site-agent:word_plan_page 而不是 "LangGraph"。
            "run_name": f"site-agent:{intent or 'auto'}",
            "tags": ["site-agent"],
            "metadata": {"thread_id": thread_id, "intent": intent or "",
                         "input_text": input_text[:200]},
        },
        snapshot=workflow_snapshot,
    )
    # trace / langsmith 都是附加字段，不属于 WorkflowState：老前端忽略也不受影响
    return {**final, "trace": trace, "langsmith": public_config()}
