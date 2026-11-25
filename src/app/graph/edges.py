# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""两条线的条件边路由：节点只产出 state，「下一步去哪」全在这里。

    常规线 normal_graph   route_after_processing / route_after_validation
    热词线 hotword_graph  route_after_fetch / route_after_word（逐词回环的闸门）
                          route_after_llm（逐词子图内：有结论才做后置动作）

写法两条线一致：路由函数返回 RouteTag，*_EDGE_MAP 把标签翻成 GraphNode 节点名，
图装配处只声明结构。取值真源在 src.app.enums，这里不写裸字符串。
"""

from __future__ import annotations

from langgraph.graph import END

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


def route_after_fetch(state: HotwordLoopState) -> str:
    """取到词才进逐词子图；候选池取尽 / KV 失败 / 名单非法 → 直接进汇总节点收尾。"""
    if state.get("load_error") or not state.get("has_more"):
        return RouteTag.SUMMARIZE
    return RouteTag.ANALYZE


FETCH_EDGE_MAP = {
    RouteTag.ANALYZE: GraphNode.ANALYZE_WORD,
    RouteTag.SUMMARIZE: GraphNode.SUMMARY,
}


def route_after_word(state: HotwordLoopState) -> str:
    """一个词跑完子图后：达上限就收尾，否则回 fetch_one 再问 tool 要一个词。

    ★「最多分析几个词」完全由此处的图路由决定，Python 调用方不写任何 while/for 驱动；
      收尾也走图内节点，不在图外做后处理。
    """
    if state.get("loop_count", 0) >= state["limit"]:
        return RouteTag.SUMMARIZE
    return RouteTag.LOOP


WORD_EDGE_MAP = {
    RouteTag.LOOP: GraphNode.FETCH_ONE,
    RouteTag.SUMMARIZE: GraphNode.SUMMARY,
}


def route_after_llm(state: HotwordLoopState) -> str:
    """逐词子图内：LLM 出了结论才走后置动作链；失败/空正文的词不写 KV，直接落账。

    映射表由装配处按本轮名单给出（NEXT → 第一个后置节点，COLLECT → collect_word），
    这里只判「有没有结论」，不认识具体步骤名。
    """
    return RouteTag.NEXT if state.get("ai") else RouteTag.COLLECT


# ── 建页线（graph/page_graph.py）：plan 失败就停；write 出正文，dry-run 到此为止 ──

def route_after_plan(state) -> str:
    """plan 失败 / 归类不出站点 → 直接收尾；否则进写作（预览也要写出真正文）。"""
    if state.get("error"):
        return RouteTag.STOP
    return RouteTag.NEXT


PAGE_PLAN_EDGE_MAP = {
    RouteTag.NEXT: GraphNode.PAGE_AUDIT,
    RouteTag.STOP: END,
}


def route_after_audit(state) -> str:
    """站情校验拦下（已有同主题页）→ 收尾；否则进写作。"""
    if state.get("error"):
        return RouteTag.STOP
    return RouteTag.NEXT


PAGE_AUDIT_EDGE_MAP = {
    RouteTag.NEXT: GraphNode.PAGE_WRITE,
    RouteTag.STOP: END,
}


def route_after_write(state) -> str:
    """正文不合格 → 收尾（渲染前拦下）；confirm=False（dry-run）看完正文就结束。"""
    if state.get("error") or not state.get("confirm"):
        return RouteTag.STOP
    return RouteTag.NEXT


PAGE_WRITE_EDGE_MAP = {
    RouteTag.NEXT: GraphNode.PAGE_RENDER,
    RouteTag.STOP: END,
}


def route_after_push(state) -> str:
    """push 成功才验活；失败（含前面渲染失败的透传）→ 直接收尾。"""
    if state.get("error"):
        return RouteTag.STOP
    return RouteTag.NEXT


PAGE_PUSH_EDGE_MAP = {
    RouteTag.NEXT: GraphNode.PAGE_VERIFY,
    RouteTag.STOP: END,
}
