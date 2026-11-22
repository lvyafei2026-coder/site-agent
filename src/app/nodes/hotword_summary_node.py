# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线终态汇总节点（与常规线 normal_output_node.py 对称）。

把逐词结果聚合成对外结构；计划非法 / KV 失败 / 空池同样在这里收口，所以无论走哪条分支，
终态都带 `summary`，运行入口直接取它做返回体，不在图外调聚合函数。
"""

from __future__ import annotations

from typing import Any

from src.app.enums import Verdict
from src.app.graph.state import HotwordLoopState
from src.app.tools.adapters.hotwords.word_result import OK_MIN_RATE


def summary_node(state: HotwordLoopState) -> dict[str, Any]:
    """聚合本轮：成功数 / 结论分布 / 本轮证据步骤与源级降级步骤。

    对外只给**步骤名**（`evidenceSteps` / `disabledSources`）——人读名是前端的事，
    后端把 label 塞进返回体就等于让 agent 认识 UI 文案。
    """
    if state.get("load_error"):
        return {"summary": {
            "ok": False, "error": state["load_error"],
            "items": [], "fetched": 0, "analyzed": 0, "failed": 0, "counts": {}}}

    results = state.get("results", [])
    if not results:
        return {"summary": {
            "ok": True, "items": [], "fetched": 0, "analyzed": 0,
            "failed": 0, "counts": {}, "note": "没有未跟进且未分析过的候选词"}}

    counts: dict[str, int] = {}
    for r in results:
        v = r["verdict"] or Verdict.UNDECIDED.value
        counts[v] = counts.get(v, 0) + 1
    analyzed = sum(1 for r in results if not r["error"])
    failed = len(results) - analyzed
    rate = analyzed / len(results)
    degrade = state["degrade"]
    evidence = list(state.get("evidence") or ())

    return {"summary": {
        "ok": rate >= OK_MIN_RATE,
        "fetched": len(results),
        "analyzed": analyzed,
        "failed": failed,
        "successRate": round(rate, 3),
        "counts": counts,
        "items": results,
        # 前端按这份名单渲染逐词表的证据列，不写死 SERP / 趋势
        "evidenceSteps": evidence,
        "disabledSources": [s for s in evidence if s in degrade.disabled],
        "llmFailures": degrade.llm_failures,
    }}
