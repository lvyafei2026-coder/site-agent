# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词循环图的**节点逻辑**：问 tool 取一词 → 分析 → 问「还有吗」→ 继续 / 结束。

这就是用户视角的固定流程，没有批次概念、没有快照队列：
    START → fetch_one → analyze_one → should_continue ─┬─ fetch_one（tool 还给得出词）
                                                      └─ END（没有 / 达上限 / KV 失败）
每轮 fetch_one 都真去问一次 tool「下一个词」，取到就分析，取不到（tool 返回 None）
就结束。循环由**图的条件边驱动**，Python 层不写 while/for。

节点名刻意用动词（FETCH_NODE / ANALYZE_NODE 公开常量），State 字段用名词
（queue / seen / results…）——langgraph 0.3.x 不允许节点名与 State 字段同名。

trace：根 span 由 graph/hotword_graph.py 的 run_hotwords 创建，root_run 经 state 显式
传给每个节点，保住「根 → 每词 → LLM」层级契约（串行下 contextvar 本可继承，显式传更稳）。

单词逻辑（补数/LLM/降级/回写）在 services/hotword_service.py 的 _analyze_one，本模块不碰。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, TypedDict

from langgraph.graph import END

from src.app.enums import Verdict
from src.app.observability.langsmith import run_traced as _span, trace_tool

from src.app.services.hotword_service import (
    _analyze_one, _DegradeState, _word_outputs, _word_error, TOOL_TRACE_PREFIX,
)
from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, term_key, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from src.app.tools.adapters.terms.hotword_kv_io import _kv_list
from src.app.tools.adapters.terms.hotword_criteria import pick_examples
from src.app.tools.adapters.terms.hotword_contract import (
    select_pending, WORD_TRACE_PREFIX, OK_MIN_RATE,
)

log = logging.getLogger(__name__)

# 节点名（动词前缀，避开 State 字段）；公开契约，供 hotword_graph 装配图时引用。
# should_continue 是条件边函数，不是节点。回环目标就是 FETCH_NODE。
FETCH_NODE = "fetch_one"
ANALYZE_NODE = "analyze_one"


class HotwordLoopState(TypedDict, total=False):
    """循环图在节点间流转的状态。字段全为名词，避免与节点名撞名。"""

    # ——入口参数（run_hotwords 构造 initial state 时注入）——
    limit: int                       # 本次最多分析的词数（= 最大循环次数）
    kv: CloudflareKV
    prompt_version: Optional[str]
    root_run: Any                    # 根 span 的 run，作每词 span 的显式父
    run_id: str
    degrade: _DegradeState           # 源级降级状态（SERP/Trends/LLM）

    # ——节点间流转——
    seen: set[str]                   # 本轮已取过的 key（防 KV 最终一致导致重复取词）
    examples_loaded: bool            # 人工判例是否已读（只读一次，挂在根 span 下）
    examples: list[dict[str, str]]
    has_more: bool                   # fetch_one 本轮是否取到词（False → 条件边收尾）
    current: dict[str, Any]          # 本轮取出的待分析词
    results: list[dict[str, Any]]
    loop_count: int
    load_error: str                  # 候选池读取失败信息（非空 → 直接 END）


def _load_examples(state: HotwordLoopState) -> dict[str, Any]:
    """首次进入时读一次人工判例，带一条 tool span（挂在根 run 下）。

    判例来自 rejected 表，一轮只读一次即可——它不随单词分析变化。
    失败只降级掉判例（examples=[]），不让整轮因这张表挂掉。
    """
    root_run = state.get("root_run")
    run_id = state.get("run_id", "")
    meta = {"run_id": run_id, "component": "analyze_hotwords"}
    examples: list[dict[str, str]] = []
    try:
        rejected_rows = trace_tool(
            lambda: _kv_list(state["kv"], REJECTED_KEY),
            name=TOOL_TRACE_PREFIX + "kv.read-examples",
            parent=root_run,
            inputs={"key": REJECTED_KEY},
            tags=["site-agent", "hotword-tool", "loop"],
            metadata=meta,
            outputs=lambda res: {"rejected": len(res) if isinstance(res, list) else 0},
        )
        examples = pick_examples(rejected_rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("判例读取失败，本轮不带判例继续：%s", str(exc)[:120])
    return {"examples_loaded": True, "examples": examples}


def fetch_one(state: HotwordLoopState) -> dict[str, Any]:
    """问 tool 要下一个词；取到就放进 current，取不到就标记 has_more=False。

    每轮都是一次真实的 tool 调用（候选查询）——没有「首次加载整个候选池到内存、
    之后游标推进」这回事。KV 最终一致可能让本轮刚取的词仍留在候选池里，故传入 seen
    排除本轮已取过的 key。
    """
    updates: dict[str, Any] = {}
    if not state.get("examples_loaded"):
        updates.update(_load_examples(state))

    root_run = state.get("root_run")
    run_id = state.get("run_id", "")
    seen: set[str] = set(state.get("seen") or set())
    try:
        item = trace_tool(
            lambda: select_pending(state["kv"], seen),
            name=TOOL_TRACE_PREFIX + "next-candidate",
            parent=root_run,
            inputs={"seenCount": len(seen), "keys": ["candidates", SAVED_KEY,
                                                     REJECTED_KEY, TAGS_KEY, AI_CACHE_PREFIX]},
            tags=["site-agent", "hotword-tool", "loop"],
            metadata={"run_id": run_id, "component": "analyze_hotwords"},
            outputs=lambda res: {"term": (res or {}).get("term", ""),
                                 "key": (res or {}).get("key", "")},
        )
    except Exception as exc:  # noqa: BLE001 - KV 不可达 → 整轮失败，不写任何状态
        log.warning("候选查询失败：%s", exc)
        return {**updates, "current": None, "has_more": False,
                "load_error": str(exc)[:200]}

    if item is None:
        return {**updates, "current": None, "has_more": False}

    seen.add(str(item["key"]))
    return {**updates, "current": item, "has_more": True, "seen": seen}


def analyze_one(state: HotwordLoopState) -> dict[str, Any]:
    """分析 current 一词，结果 append；无词（tool 已取尽）时是 no-op。"""
    item = state.get("current")
    results = list(state.get("results", []))
    loop_count = state.get("loop_count", 0)

    # 条件边保证只在有词时走到这里；取尽时 current=None，直接放过。
    if not item:
        return {"results": results, "loop_count": loop_count}

    kv = state["kv"]
    root_run = state.get("root_run")
    run_id = state.get("run_id", "")
    idx = loop_count
    term = str(item.get("term") or "").strip()
    key = term_key(item.get("key") or term)
    degrade = state.get("degrade")  # type: ignore[assignment]  # _DegradeState 实例

    result = _span(
        lambda word_run: _analyze_one(
            item, kv, degrade, word_run,
            state.get("prompt_version"), state.get("examples", [])),
        name=WORD_TRACE_PREFIX + (term or key)[:60],
        parent=root_run,
        inputs={"term": term, "key": key, "index": idx},
        tags=["site-agent", "hotword", "loop"],
        metadata={
            "run_id": run_id,
            "thread_id": run_id,
            "word_id": f"{run_id}/{idx}/{key}",
            "term": term, "key": key, "index": idx,
        },
        outputs=_word_outputs,
        error=_word_error,
        pass_run=True,
    )
    results.append(result)
    return {"results": results, "loop_count": loop_count + 1}


def should_continue(state: HotwordLoopState) -> str:
    """条件边：KV 失败 / 没词了 / 达上限 → END；否则回 fetch_one 再问 tool 要一个。

    ★这是循环唯一的闸门——「最多分析几个词」完全由此处的图路由决定，
      Python 调用方不写任何 while/for 驱动。
    """
    if state.get("load_error"):
        return END
    if state.get("loop_count", 0) >= state["limit"]:
        return END
    if not state.get("has_more"):
        return END
    return FETCH_NODE


def summarize(state: HotwordLoopState) -> dict[str, Any]:
    """把终态聚合成对外结构；KV 失败/空池也在这里收口。"""
    if state.get("load_error"):
        return {"ok": False, "error": "候选查询失败：" + state["load_error"],
                "items": [], "fetched": 0, "analyzed": 0, "failed": 0, "counts": {}}

    results = state.get("results", [])
    if not results:
        return {"ok": True, "items": [], "fetched": 0, "analyzed": 0,
                "failed": 0, "counts": {}, "note": "没有未跟进且未分析过的候选词"}

    counts: dict[str, int] = {}
    for r in results:
        v = r["verdict"] or Verdict.UNDECIDED.value
        counts[v] = counts.get(v, 0) + 1
    analyzed = sum(1 for r in results if not r["error"])
    failed = len(results) - analyzed
    rate = analyzed / len(results)
    degrade = state.get("degrade")

    out = {
        "ok": rate >= OK_MIN_RATE,
        "fetched": len(results),
        "analyzed": analyzed,
        "failed": failed,
        "successRate": round(rate, 3),
        "counts": counts,
        "items": results,
        "serpDisabled": bool(degrade and degrade.serp_off),  # type: ignore[union-attr]
        "trendsDisabled": bool(degrade and degrade.trends_off),  # type: ignore[union-attr]
        "llmFailures": int(degrade.llm_failures) if degrade else 0,  # type: ignore[union-attr]
    }
    degraded = []
    if out["serpDisabled"]:
        degraded.append("SERP 源不可用，本轮已跳过 SERP（结论缺少辨形线索）")
    if out["trendsDisabled"]:
        degraded.append("趋势源不可用，本轮已禁用趋势")
    if degraded:
        out["note"] = "；".join(degraded) + "；其余词仍照常分析并回写"
    return out
