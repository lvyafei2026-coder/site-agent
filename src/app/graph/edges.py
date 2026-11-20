# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""两条线的条件边路由：节点只产出 state，「下一步去哪」全在这里。

    常规线 normal_graph   route_after_processing / route_after_validation
    热词线 hotword_graph  route_after_analyze（逐词回环唯一的闸门）

写法两条线一致：路由函数返回 RouteTag，*_EDGE_MAP 把标签翻成 GraphNode 节点名，
图装配处只声明结构。取值真源在 src.app.enums，这里不写裸字符串。
"""

from __future__ import annotations

from src.app.config import settings
from src.app.enums import GraphNode, RouteTag
from src.app.graph.state import HotwordLoopState, WorkflowState


def route_after_processing(state: WorkflowState) -> str:
    """processing 置 processing_fatal（明确的非网络失败）→ 直接 error，跳过校验与重试；
    否则（含网络/连接层瞬态故障）→ 进 validate，由 validation 走重试逻辑。

    与 route_after_validation 的分工：
    - 这里判的是「**节点级硬失败**」：LLM 鉴权/配额/参数错、工具返回明确非网络失败、
      工具抛未捕获业务异常。这些重试没用，直接结束等下次请求重试；
    - 网络超时（LLM 连接超时、工具 ConnectTimeout/SourceUnavailable）不置 fatal，
      走 validate → 重试，连重试多轮仍失败才由 validation 落 ERROR；
    - 软失败（字段偏少、部分工具 ok:False 但网络类）同样交给 validation 决定。
    """
    if state.get("processing_fatal"):
        return RouteTag.ERROR
    return RouteTag.VALIDATE


PROCESSING_EDGE_MAP = {
    RouteTag.VALIDATE: GraphNode.VALIDATION,
    RouteTag.ERROR: GraphNode.ERROR,
}


def route_after_validation(state: WorkflowState) -> str:
    """校验通过 → build_output；还能重试 → run_processing；否则 → handle_error。"""
    if state.get("validation", {}).get("is_valid"):
        return RouteTag.COMPLETE
    if state.get("retry_count", 0) < settings.max_validation_retries:
        return RouteTag.RETRY
    return RouteTag.ERROR


EDGE_MAP = {
    RouteTag.COMPLETE: GraphNode.OUTPUT,
    RouteTag.RETRY: GraphNode.PROCESSING,
    RouteTag.ERROR: GraphNode.ERROR,
}


def route_after_analyze(state: HotwordLoopState) -> str:
    """KV 失败 / 没词了 / 达上限 → build_summary 收尾；否则回 fetch_one 再问 tool。

    ★这是循环唯一的闸门——「最多分析几个词」完全由此处的图路由决定，
      Python 调用方不写任何 while/for 驱动。收尾也走图内节点，不在图外后处理。
    """
    if state.get("load_error"):
        return RouteTag.SUMMARIZE
    if state.get("loop_count", 0) >= state["limit"]:
        return RouteTag.SUMMARIZE
    if not state.get("has_more"):
        return RouteTag.SUMMARIZE
    return RouteTag.LOOP


HOTWORD_EDGE_MAP = {
    RouteTag.LOOP: GraphNode.FETCH_ONE,
    RouteTag.SUMMARIZE: GraphNode.SUMMARY,
}
