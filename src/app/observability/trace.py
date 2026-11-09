# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""图执行轨迹：把一轮 LangGraph 实际走过的节点、每步耗时、状态变化抓出来。

为什么不用 `graph.stream(stream_mode="values")`：每一步都会回传完整 state，
而 tool_results 里可能塞着 1-2MB 的热词列表，一路复制只会拖慢单轮。这里只取
`updates`（每节点的增量），在本地合并成终态——本项目所有节点返回的都是完整
字段值（如 `messages` 回整条列表），所以 `{**acc, **update}` 与官方终态一致。

产出结构（随 /run 返回的 `trace` 字段）：

    {
      "path": "classify_intent → run_processing → validate_result → build_output",
      "nodes": [{"node": "classify_intent", "ms": 8,
                 "changed": {"intent": "select_pages", "confidence": 1.0}}, ...],
      "totalMs": 1420,
      "route": "complete"          # complete / error；有重试时会有多个 run_processing
    }
"""

from __future__ import annotations

import time
from typing import Any

# 每步只抽这些「有语义」的字段做 diff；大对象（tool_results / extracted_data）
# 一律降维成计数，避免把几 MB 数据塞进 trace。
_MAX_TEXT = 120


def _clip(value: str) -> str:
    return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…"


def _snapshot(state: dict) -> dict[str, Any]:
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


def _diff(before: dict, after: dict) -> dict[str, Any]:
    """只留变化的字段，空值不展示（首次出现的 False 要留，代表校验没过）。"""
    changed = {}
    for key, value in after.items():
        if value == before.get(key) or value == "":
            continue
        changed[key] = value
    return changed


def run_traced(graph, state: dict, config: dict) -> tuple[dict, dict]:
    """跑一轮并返回 (终态, 轨迹)。终态与 graph.invoke 等价。"""
    started = time.perf_counter()
    acc: dict = dict(state)
    prev = _snapshot(acc)
    steps: list[dict] = []
    mark = started

    for chunk in graph.stream(state, config=config, stream_mode="updates"):
        now = time.perf_counter()
        for node, update in chunk.items():
            if isinstance(update, dict):
                acc.update(update)
            snap = _snapshot(acc)
            steps.append({
                "node": node,
                "ms": int((now - mark) * 1000),
                "changed": _diff(prev, snap),
            })
            prev, mark = snap, now

    last = steps[-1]["node"] if steps else ""
    trace = {
        "path": " → ".join(s["node"] for s in steps),
        "nodes": steps,
        "totalMs": int((time.perf_counter() - started) * 1000),
        "route": "error" if last == "handle_error" else "complete",
        "retried": sum(1 for s in steps if s["node"] == "run_processing") > 1,
    }
    return acc, trace
