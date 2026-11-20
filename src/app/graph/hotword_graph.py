# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词循环图：装配 + 运行入口，一个固定流程，没有 batch 概念。

图结构（fetch_one 每轮问 tool 要一个词，处理完再问「还有吗」）：
    START → fetch_one → analyze_one → should_continue ─┬─ fetch_one（还有）
                                                      └─ END（没有 / 达上限 / KV 失败）

run_hotwords() 是唯一运行入口：构造 state → invoke 图 → summarize。
节点逻辑在 nodes/hotword_nodes.py，单词分析在 services/hotword_service.py，
取词/判据/IO 在 tools/adapters/terms/ 叶子层。依赖全程单向向下，无环、无函数内 import。

trace 是声明式切面（observability.observed）：根 span 由函数定义处的 @observed 声明，
回链由 link="traceRun" 自动注入返回体；节点/词/IO 的 span 靠 contextvar 自动挂在根下。
"""

from __future__ import annotations

import time
import uuid
from functools import lru_cache
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from src.app.config import settings
from src.app.nodes import hotword_nodes as _nodes
from src.app.observability.langsmith import observed
from src.app.services.hotword_service import _DegradeState
from src.app.tools.adapters.cloudflare.kv import get_kv
from src.app.tools.adapters.terms.hotword_contract import RUN_TRACE_NAME


def build_hotword_graph():
    """装配逐词循环图：取一词 → 分析 → 条件边决定回环还是结束。

    循环完全由条件边驱动，Python 层不写 while/for。不传 checkpointer：
    一次 invoke 跑完即弃，不跨请求续跑。
    """
    graph = StateGraph(_nodes.HotwordLoopState)
    graph.add_node(_nodes.FETCH_NODE, _nodes.fetch_one)
    graph.add_node(_nodes.ANALYZE_NODE, _nodes.analyze_one)
    graph.add_edge(START, _nodes.FETCH_NODE)
    graph.add_edge(_nodes.FETCH_NODE, _nodes.ANALYZE_NODE)
    graph.add_conditional_edges(
        _nodes.ANALYZE_NODE,
        _nodes.should_continue,
        {_nodes.FETCH_NODE: _nodes.FETCH_NODE, END: END},
    )
    return graph.compile()


@lru_cache(maxsize=1)
def get_hotword_graph():
    """热词图进程级单例（图无状态，运行时数据全在 invoke 的 state 里）。"""
    return build_hotword_graph()


def _run_inputs(limit: int = 20, prompt_version: Optional[str] = None) -> dict[str, Any]:
    return {
        "limit": limit,
        "promptVersion": prompt_version
        or (settings.default_prompt_versions or {}).get("hotwords", "v1"),
    }


def _run_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """根 span 的 outputs：LangSmith 上直接看成功数与降级情况，不用点开 items。"""
    keys = ("ok", "fetched", "analyzed", "failed", "successRate", "counts",
            "serpDisabled", "trendsDisabled", "llmFailures", "error", "note")
    return {k: out[k] for k in keys if k in out}


@observed(RUN_TRACE_NAME, kind="chain", inputs=_run_inputs,
          outputs=_run_outputs, tags=["site-agent", "hotword-run"], link="traceRun")
def run_hotwords(limit: int = 20,
                 prompt_version: Optional[str] = None) -> dict[str, Any]:
    """跑一轮热词分析：问 tool 逐词取 → 逐词分析 → 没有更多词或达上限就结束。

    limit 是最大循环次数（1-50）。KV 候选池不可达时整轮失败，不写任何状态。
    每个词独立跑完、失败只落在该词 items[].error；源级故障只降级「取数」这一步。
    trace 由装饰器切面承担，函数体只有纯业务：构造 state → invoke → summarize。
    """
    limit = max(1, min(int(limit or 20), 50))
    run_id = f"hotwords-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    initial: _nodes.HotwordLoopState = {
        "limit": limit,
        "kv": get_kv(),
        "prompt_version": prompt_version,
        "degrade": _DegradeState(),
        "seen": set(),
        "examples_loaded": False,
        "examples": [],
        "has_more": False,
        "current": None,
        "results": [],
        "loop_count": 0,
    }
    # 循环图每词 2 个 super-step（fetch + analyze），取尽那轮再多 1 次 fetch；
    # limit 最大 50 → 最坏 2*50+2 = 102。给足上限，让 should_continue 决定收尾。
    out = _nodes.summarize(get_hotword_graph().invoke(
        initial, config={"recursion_limit": limit * 2 + 4}))
    out["runId"] = run_id
    return out
