# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""图执行轨迹：两条线共用的一个驱动器 + 各自的字段快照。

`run_traced` 是唯一的图执行方式（常规线 /run 与热词线 /hotwords 都走它）：
把一轮 LangGraph 实际走过的节点、每步耗时、状态变化抓出来，返回 (终态, 轨迹)。
两条线的差别只在**快照提取器**——即「这条线的 state 里哪些字段有语义」：

    workflow_snapshot   常规线：意图 / 置信度 / 工具成功数 / 校验 / 输出
    hotword_snapshot    热词线：当前词 / 轮次 / 成功数 / 结论 / 是否还有词

为什么不用 `graph.stream(stream_mode="values")`：每一步都会回传完整 state，
而 tool_results 里可能塞着 1-2MB 的热词列表，一路复制只会拖慢单轮。这里只取
`updates`（每节点的增量），在本地合并成终态——本项目所有节点返回的都是完整
字段值（如 `messages` 回整条列表），所以 `{**acc, **update}` 与官方终态一致。

产出结构（随两条线的返回体一起给前端的 `trace` 字段）：

    {
      "path": "classify_intent → run_processing → validate_result → build_output",
      "nodes": [{"node": "classify_intent", "ms": 8,
                 "changed": {"intent": "select_pages", "confidence": 1.0}}, ...],
      "totalMs": 1420,
      "route": "complete",         # complete / error：终态是否带错误收尾
      "looped": false              # 有节点被重复执行（常规线=重试，热词线=逐词回环）
    }
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any, Callable

from src.app.enums import GraphNode, RouteTag

# 每步只抽这些「有语义」的字段做 diff；大对象（tool_results / extracted_data / results）
# 一律降维成计数，避免把几 MB 数据塞进 trace。
_MAX_TEXT = 120


def _clip(value: str) -> str:
    return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…"


def workflow_snapshot(state: dict) -> dict[str, Any]:
    """常规线快照：WorkflowState 里有语义的字段。"""
    results = state.get("tool_results") or {}
    ok = sum(1 for r in results.values() if isinstance(r, dict) and r.get("ok") is not False)
    validation = state.get("validation") or {}
    extracted = state.get("extracted_data") or {}
    return {
        "intent": state.get("intent"),
        "confidence": state.get("confidence"),
        "retry": state.get("retry_count"),
        "tools": f"{ok}/{len(results)} ok" if results else "",
        "valid": validation.get("is_valid"),
        "reason": _clip(str(validation.get("reason") or "")),
        "fields": len(extracted) or "",
        "messages": len(state.get("messages") or []) or "",
        "output": _clip(str(state.get("output") or "")),
        "error": _clip(str(state.get("error") or "")),
    }


def hotword_snapshot(state: dict) -> dict[str, Any]:
    """热词线快照：HotwordLoopState 里有语义的字段（逐词明细只留计数与最后一条结论）。"""
    results = state.get("results") or []
    ok = sum(1 for r in results if not r.get("error"))
    current = state.get("current") or {}
    summary = state.get("summary") or {}
    return {
        "term": current.get("term", ""),
        "loop": state.get("loop_count", 0),
        "words": f"{ok}/{len(results)} ok" if results else "",
        "verdict": (results[-1].get("verdict") if results else "") or "",
        "has_more": state.get("has_more"),
        "error": _clip(str(summary.get("error") or state.get("load_error") or "")),
    }


def _diff(before: dict, after: dict) -> dict[str, Any]:
    """只留变化的字段，空值不展示（首次出现的 False 要留，代表校验没过 / 已取尽）。"""
    changed = {}
    for key, value in after.items():
        if value == before.get(key) or value == "":
            continue
        changed[key] = value
    return changed


def run_traced(graph, state: dict, config: dict, *,
               snapshot: Callable[[dict], dict[str, Any]]) -> tuple[dict, dict]:
    """跑一轮并返回 (终态, 轨迹)。终态与 graph.invoke 等价。

    两条线唯一的差别是传入的 snapshot：它决定每步展示哪些字段，也提供 `error`
    供 route 判定。驱动器本身不认识任何一条线的业务字段。
    """
    started = time.perf_counter()
    acc: dict = dict(state)
    prev = snapshot(acc)
    steps: list[dict] = []
    mark = started

    for chunk in graph.stream(state, config=config, stream_mode="updates"):
        now = time.perf_counter()
        for node, update in chunk.items():
            if isinstance(update, dict):
                acc.update(update)
            snap = snapshot(acc)
            steps.append({
                "node": node,
                "ms": int((now - mark) * 1000),
                "changed": _diff(prev, snap),
            })
            prev, mark = snap, now

    last = steps[-1]["node"] if steps else ""
    visits = Counter(s["node"] for s in steps)
    trace = {
        "path": " → ".join(s["node"] for s in steps),
        "nodes": steps,
        "totalMs": int((time.perf_counter() - started) * 1000),
        "route": (RouteTag.ERROR.value
                  if last == GraphNode.ERROR.value or prev.get("error")
                  else RouteTag.COMPLETE.value),
        "looped": any(n > 1 for n in visits.values()),
    }
    return acc, trace
