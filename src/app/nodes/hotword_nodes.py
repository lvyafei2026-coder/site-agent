# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词逐词循环图的**节点逻辑**：一次读一词 → 分析 → 回环，直到词用完或达上限。

2026-11-20 分层归位：节点逻辑从 adapters/terms/hotword_loop.py 上移到 nodes 层。
本模块只放「节点做什么」（状态 / fetch_one / analyze_one / should_continue / summarize），
**不装配图、不认识 batch**：图的结构由 graph/hotword_graph.py 装配，单向依赖（graph → 本模块）。

循环由**图的条件边驱动**，不是 Python 层的 while/for（结构见 graph/hotword_graph.py）：
    START → fetch_one → analyze_one → should_continue ─┬─ fetch_one（还有词且未达上限）
                                                      └─ END（词用完 / 达上限 / KV 失败）

节点名刻意用动词（FETCH_NODE / ANALYZE_NODE 公开常量），State 字段用名词（queue / cursor /
results ...）——langgraph 0.3.x 不允许 add_node 的节点名与 State 字段名同名。

trace：批次 span 由 graph/hotword_batch.py 的外壳创建，batch_run 经 state 显式传给
每个节点；串行执行下 contextvar 虽不丢，仍显式传以保住「批次→词→LLM」层级契约。

单词逻辑（补数/LLM/降级/回写）由 services/hotword_service.py 的 _analyze_one 承担，本模块不碰。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, TypedDict

from langgraph.graph import END

from src.app.enums import Verdict
from src.app.observability.langsmith import run_traced as _span, trace_tool

from src.app.services.hotword_service import (
    _analyze_one, _BatchAbort, _word_outputs, _word_error, TOOL_TRACE_PREFIX,
)
from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, term_key, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from src.app.tools.adapters.terms.hotword_kv_io import _kv_list
from src.app.tools.adapters.terms.hotword_criteria import pick_examples
from src.app.tools.adapters.terms.hotword_contract import (
    _select_pending, WORD_TRACE_PREFIX, BATCH_OK_MIN_RATE,
)

log = logging.getLogger(__name__)

# 节点名（动词前缀，避开 State 字段）；公开契约，供 builder 装配图时引用。
# should_continue 是条件边函数，不是节点。回环目标就是 FETCH_NODE，无需单独的 _RETRY 常量。
FETCH_NODE = "fetch_one"
ANALYZE_NODE = "analyze_one"


class HotwordLoopState(TypedDict, total=False):
    """循环图在节点间流转的状态。字段全为名词，避免与节点名撞名。"""

    # ——入口参数（run_batch 构造 initial state 时注入）——
    limit: int                       # 本次最多分析的词数（= 最大循环次数）
    kv: CloudflareKV
    prompt_version: Optional[str]
    batch_run: Any                   # 批次 span 的 run，作每词 span 的显式父
    batch_id: str
    abort: _BatchAbort               # 批次级降级状态（SERP/Trends/LLM）

    # ——节点间流转——
    loaded: bool                     # 候选池/判例是否已懒加载（fetch_one 首次置位）
    queue: list[dict[str, Any]]      # 待分析候选
    cursor: int                      # 下一个待取下标（取词时 +1，故本轮词下标 = cursor-1）
    results: list[dict[str, Any]]    # 每词分析结果
    loop_count: int                  # 已分析词数（受 limit 约束）
    current: dict[str, Any]          # 本轮 fetch 取出、待 analyze 的词
    examples: list[dict[str, str]]   # 人工判例（整批只读一次）
    load_error: str                  # 候选池读取失败信息（非空 → 直接 END）


def _load_once(state: HotwordLoopState) -> dict[str, Any]:
    """首次进入时读候选池 + 判例，各带一条 tool span（挂在批次 run 下）。

    返回需要合并进 state 的字段；失败不抛，写进 load_error 让条件边收尾。
    """
    kv = state["kv"]
    limit = state["limit"]
    batch_run = state.get("batch_run")
    batch_id = state.get("batch_id", "")

    meta = {"batch_id": batch_id, "component": "analyze_hotwords"}
    try:
        queue = trace_tool(
            lambda: _select_pending(kv, limit),
            name=TOOL_TRACE_PREFIX + "kv.read-candidates",
            parent=batch_run,
            inputs={"limit": limit,
                    "keys": ["candidates", SAVED_KEY, REJECTED_KEY, TAGS_KEY,
                             AI_CACHE_PREFIX]},
            tags=["site-agent", "hotword-tool", "loop"],
            metadata=meta,
            outputs=lambda res: {
                "selected": len(res),
                "terms": [x.get("term", "") for x in res],
                "keys": [x.get("key", "") for x in res],
            },
        )
    except Exception as exc:  # noqa: BLE001 - KV 不可达 → 整批失败，不写任何状态
        log.warning("候选池读取失败：%s", exc)
        return {"loaded": True, "queue": [], "load_error": str(exc)[:200]}

    # 判例失败只降级掉判例（examples=[]），不让整批因这张表挂掉。
    examples: list[dict[str, str]] = []
    try:
        rejected_rows = trace_tool(
            lambda: _kv_list(kv, REJECTED_KEY),
            name=TOOL_TRACE_PREFIX + "kv.read-examples",
            parent=batch_run,
            inputs={"key": REJECTED_KEY},
            tags=["site-agent", "hotword-tool", "loop"],
            metadata=meta,
            outputs=lambda res: {"rejected": len(res) if isinstance(res, list) else 0},
        )
        examples = pick_examples(rejected_rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("判例读取失败，本批不带判例继续：%s", str(exc)[:120])
    return {"loaded": True, "queue": queue, "examples": examples}


def fetch_one(state: HotwordLoopState) -> dict[str, Any]:
    """首次懒加载候选池/判例；随后从队列取下一条词作为 current。"""
    updates: dict[str, Any] = {}
    if not state.get("loaded"):
        updates.update(_load_once(state))
        queue = updates.get("queue", [])
    else:
        queue = state.get("queue", [])

    if updates.get("load_error"):
        updates.update({"current": None, "cursor": state.get("cursor", 0)})
        return updates

    cursor = state.get("cursor", 0)
    if cursor < len(queue):
        updates.update({
            "current": queue[cursor],
            "cursor": cursor + 1,
        })
    else:
        updates.update({"current": None, "cursor": cursor})
    return updates


def analyze_one(state: HotwordLoopState) -> dict[str, Any]:
    """分析 current 一词，结果 append；无词（空池/达上限）时是 no-op。"""
    item = state.get("current")
    results = list(state.get("results", []))
    loop_count = state.get("loop_count", 0)

    # 条件边保证只在有词时走到这里；空池/达上限时 current=None，直接放过。
    if not item:
        return {"results": results, "loop_count": loop_count}

    kv = state["kv"]
    batch_run = state.get("batch_run")
    batch_id = state.get("batch_id", "")
    idx = state.get("cursor", 0) - 1   # fetch_one 取词后已把 cursor +1，故本轮词下标 = cursor-1
    term = str(item.get("term") or "").strip()
    key = term_key(item.get("key") or term)
    abort = state.get("abort")  # type: ignore[assignment]  # _BatchAbort 实例

    result = _span(
        lambda word_run: _analyze_one(
            item, kv, abort, word_run,
            state.get("prompt_version"), state.get("examples", [])),
        name=WORD_TRACE_PREFIX + (term or key)[:60],
        parent=batch_run,
        inputs={"term": term, "key": key, "index": idx},
        tags=["site-agent", "hotword", "loop"],
        metadata={
            "batch_id": batch_id,
            "thread_id": batch_id,
            "word_id": f"{batch_id}/{idx}/{key}",
            "term": term, "key": key, "index": idx,
        },
        outputs=_word_outputs,
        error=_word_error,
        pass_run=True,
    )
    results.append(result)
    return {"results": results, "loop_count": loop_count + 1}


def should_continue(state: HotwordLoopState) -> str:
    """条件边：KV 失败 / 无词 / 达上限 → END；否则回 fetch_one 继续下一词。

    ★这是循环唯一的闸门——「最多分析几个词」完全由此处的图路由决定，
      Python 调用方不写任何 while/for 驱动。
    """
    if state.get("load_error"):
        return END
    if state.get("loop_count", 0) >= state["limit"]:
        return END
    if state.get("cursor", 0) >= len(state.get("queue", [])):
        return END
    return FETCH_NODE


def summarize(state: HotwordLoopState) -> dict[str, Any]:
    """把终态聚合回 run_batch 的对外结构；KV 失败/空池也在这里收口。"""
    if state.get("load_error"):
        return {"ok": False, "error": "候选池读取失败：" + state["load_error"],
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
    abort = state.get("abort")

    out = {
        "ok": rate >= BATCH_OK_MIN_RATE,
        "fetched": len(results),
        "analyzed": analyzed,
        "failed": failed,
        "successRate": round(rate, 3),
        "counts": counts,
        "items": results,
        "serpDisabled": bool(abort and abort.serp_off),  # type: ignore[union-attr]
        "trendsDisabled": bool(abort and abort.trends_off),  # type: ignore[union-attr]
        "llmFailures": int(abort.llm_failures) if abort else 0,  # type: ignore[union-attr]
    }
    degraded = []
    if out["serpDisabled"]:
        degraded.append("SERP 源不可用，本批已跳过 SERP（结论缺少辨形线索）")
    if out["trendsDisabled"]:
        degraded.append("趋势源不可用，本批已禁用趋势")
    if degraded:
        out["note"] = "；".join(degraded) + "；其余词仍照常分析并回写"
    return out
