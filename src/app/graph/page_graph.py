# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""建页子图：装配 + 运行入口（与 normal_graph / hotword_graph 同构，第三条并列线）。

    START → page_plan → route_after_plan ─┬─ page_render → page_push
                                          │                 → route_after_push ─┬─ page_verify → END
                                          └ END（预览 / 失败 / 归类不出站点）     └ END

一次只建一页（词级批量由调用方多次触发）；confirm=False 是 dry-run——plan 出
页面结构与目标站就收尾，一个写动作都不发生。push 成功后台账已由工具层自动
UPSERT（publishing_tools.git_push），verify 只更新 live_ok，不再写 KV。

依赖单向向下：graph → nodes → services → tools → adapters；节点不写工具名字面量，
render / push / verify 的路由在 tool_service.PAGE_STEP_TOOLS。
"""

from __future__ import annotations

import time
import uuid
from functools import lru_cache
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from src.app.enums import GraphNode, TraceName
from src.app.graph.edges import PAGE_PLAN_EDGE_MAP, PAGE_PUSH_EDGE_MAP, route_after_plan, route_after_push
from src.app.graph.state import PageBuildState, initial_page_build_state
from src.app.nodes.page_nodes import page_plan, page_push, page_render, page_verify
from src.app.observability.langsmith import observed, public_config
from src.app.observability.trace import page_snapshot, run_traced
from src.app.services.page_build_service import todo_pool_size


def build_page_graph():
    """固定串形：plan（取词+归类+解析）→ render → push → verify。无 checkpointer（一次跑完即弃）。"""
    graph = StateGraph(PageBuildState)
    for node, fn in ((GraphNode.PAGE_PLAN, page_plan), (GraphNode.PAGE_RENDER, page_render),
                     (GraphNode.PAGE_PUSH, page_push), (GraphNode.PAGE_VERIFY, page_verify)):
        graph.add_node(node, fn)

    graph.add_edge(START, GraphNode.PAGE_PLAN)
    graph.add_conditional_edges(GraphNode.PAGE_PLAN, route_after_plan, PAGE_PLAN_EDGE_MAP)
    graph.add_edge(GraphNode.PAGE_RENDER, GraphNode.PAGE_PUSH)
    graph.add_conditional_edges(GraphNode.PAGE_PUSH, route_after_push, PAGE_PUSH_EDGE_MAP)
    graph.add_edge(GraphNode.PAGE_VERIFY, END)
    return graph.compile()


@lru_cache(maxsize=1)
def get_page_graph():
    """进程级单例（图无状态，数据全在每轮的 state 里）。"""
    return build_page_graph()


def _run_inputs(term: str = "", confirm: bool = False) -> dict[str, Any]:
    return {"term": term, "confirm": confirm}


def _run_outputs(out: dict[str, Any]) -> dict[str, Any]:
    # run_page_build 的返回体是 summary 的平铺（外加 page/runId/trace），这里只挑业务键。
    keys = ("ok", "stage", "term", "site", "slug", "url", "liveOk", "classifyBy",
            "todoPool", "error", "note")
    return {k: out[k] for k in keys if k in out}


def _summarize(state: PageBuildState) -> dict[str, Any]:
    """终态汇总：dry-run 与失败都是正常输出（ok=True + stage 说明走到哪），只有异常才 ok=False。"""
    page = state.get("page") or {}
    error = state.get("error") or ""
    stage = state.get("stage") or "plan"
    summary: dict[str, Any] = {
        "ok": not (error and stage in ("render", "push", "verify")),
        "stage": stage,
        "term": (state.get("picked") or {}).get("term") or state.get("term") or "",
        "site": state.get("site") or "",
        "classifyBy": state.get("classify_by") or "",
        "slug": page.get("slug") or "",
        "todoPool": todo_pool_size(),
        "dryRun": not state.get("confirm"),
    }
    if state.get("sha"):
        summary["sha"] = state["sha"]
    if state.get("url"):
        summary["url"] = state["url"]
    if stage == "verify":
        summary["liveOk"] = bool(state.get("live_ok"))
    if error:
        summary["error"] = error
    if state.get("confirm") and not error and stage == "verify":
        summary["note"] = "页面已推送并验活，台账状态 deployed（verified 由下一次验活更新）"
    elif not state.get("confirm") and not error:
        summary["note"] = "dry-run 预览：confirm=true 才会真正渲染并推送"
    return summary


@observed(TraceName.PAGE_RUN, kind="chain", inputs=_run_inputs,
          outputs=_run_outputs, tags=["site-agent", "page-run"], link="traceRun")
def run_page_build(term: str = "", confirm: bool = False) -> dict[str, Any]:
    """跑一轮建页：一条待开发词 → 归类站点 → 渲染 → 推送 → 验活。

    term 为空 = 自动取下一条带分析正文的 todo 词；confirm=False 只出预览。
    """
    run_id = f"page-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    initial = initial_page_build_state(term, confirm)
    final, trace = run_traced(
        get_page_graph(),
        initial,
        config={
            "recursion_limit": 16,
            "tags": ["site-agent"],
            "metadata": {"runId": run_id, "term": term or "", "confirm": confirm},
        },
        snapshot=page_snapshot,
    )
    summary = _summarize(final)
    return {**summary, "page": final.get("page") or {}, "runId": run_id,
            "trace": trace, "langsmith": public_config()}
