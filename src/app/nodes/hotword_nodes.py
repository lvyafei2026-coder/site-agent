# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词循环图的**节点逻辑**：问 tool 取一词 → 分析 → 问「还有吗」→ 继续 / 结束。

    START → fetch_one → analyze_one → should_continue ─┬─ fetch_one（tool 还给得出词）
                                                      └─ END（没有 / 达上限 / KV 失败）

每轮 fetch_one 都真去问一次 tool「下一个词」，取到就分析，取不到（None）就结束。
循环由**图的条件边驱动**，Python 层不写 while/for。

节点名刻意用动词（FETCH_NODE / ANALYZE_NODE 公开常量），State 字段用名词，
避免 langgraph 0.3.x「节点名与 State 字段同名」的限制。

trace 是声明式切面：取词 / 判例读在函数定义处用 @observed 声明 tool span；
analyze_one 直接调已被切面装饰的 _analyze_one。父子靠 contextvar 自动继承，
节点里没有任何 parent / run_id / outputs 拼装代码。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, TypedDict

from langgraph.graph import END

from src.app.enums import Verdict
from src.app.observability.langsmith import observed

from src.app.services.hotword_service import _analyze_one, _DegradeState
from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from src.app.tools.adapters.terms.hotword_kv_io import _kv_list
from src.app.tools.adapters.terms.hotword_criteria import pick_examples
from src.app.tools.adapters.terms.hotword_contract import (
    select_pending, OK_MIN_RATE, TOOL_KV_PREFIX,
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
    degrade: _DegradeState           # 源级降级状态（SERP/Trends/LLM）

    # ——节点间流转——
    seen: set[str]                   # 本轮已取过的 key（防 KV 最终一致导致重复取词）
    examples_loaded: bool            # 人工判例是否已读（只读一次）
    examples: list[dict[str, str]]
    has_more: bool                   # fetch_one 本轮是否取到词（False → 条件边收尾）
    current: dict[str, Any]          # 本轮取出的待分析词
    results: list[dict[str, Any]]
    loop_count: int
    load_error: str                  # 候选池读取失败信息（非空 → 直接 END）


@observed(TOOL_KV_PREFIX + "read-examples", kind="tool",
          inputs=lambda kv: {"key": REJECTED_KEY},
          outputs=lambda rows: {"rejected": len(rows) if isinstance(rows, list) else 0})
def _read_examples(kv: CloudflareKV) -> list:
    return _kv_list(kv, REJECTED_KEY)


@observed(TOOL_KV_PREFIX + "next-candidate", kind="tool",
          inputs=lambda kv, seen: {
              "seenCount": len(seen),
              "keys": ["candidates", SAVED_KEY, REJECTED_KEY, TAGS_KEY, AI_CACHE_PREFIX],
          },
          outputs=lambda res: {"term": (res or {}).get("term", ""),
                               "key": (res or {}).get("key", "")})
def _next_candidate(kv: CloudflareKV, seen: set[str]) -> dict[str, Any] | None:
    return select_pending(kv, seen)


def _load_examples(state: HotwordLoopState) -> dict[str, Any]:
    """首次进入时读一次人工判例；失败只降级掉判例，不让整轮因这张表挂掉。"""
    try:
        examples = pick_examples(_read_examples(state["kv"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("判例读取失败，本轮不带判例继续：%s", str(exc)[:120])
        examples = []
    return {"examples_loaded": True, "examples": examples}


def fetch_one(state: HotwordLoopState) -> dict[str, Any]:
    """问 tool 要下一个词；取到就放进 current，取不到就标记 has_more=False。

    每轮都是一次真实的 tool 调用——没有「首次加载整个候选池、之后游标推进」。
    KV 最终一致可能让刚取的词仍留在候选池里，故传 seen 排除本轮已取过的 key。
    """
    updates: dict[str, Any] = {}
    if not state.get("examples_loaded"):
        updates.update(_load_examples(state))

    seen: set[str] = set(state.get("seen") or set())
    try:
        item = _next_candidate(state["kv"], seen)
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

    result = _analyze_one(
        item, state["kv"], state.get("degrade"),
        state.get("prompt_version"), state.get("examples", []),
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
