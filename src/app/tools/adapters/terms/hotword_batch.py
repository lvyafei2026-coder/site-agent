# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的批次外壳：批次 trace 根 + 对外契约 + 循环图加载边界。

本文件是热词子系统对外的执行入口（run_batch）。单词内部（补数/LLM/回写）在
hotword_worker.py，取数在 hotword_fetch.py，候选筛选规则与 trace 常量在
共享叶子 hotword_contract.py。

★逐词调度由 graph/builder.py 装配的 StateGraph 回环图承担（fetch_one → analyze_one
  → should_continue 回环）；节点逻辑在 hotword_loop.py。本文件只做三件事：
  构造循环图 initial state → invoke → summarize；外层套批次 trace + traceRun 回链。
  取图经 _hotword_graph() 的加载期边界（builder 顶层拉起整条主图链，不能顶层 import）。

★2026-11-13：不再有「整批中止」。每个词独立跑完并把自己的失败写进 items[].error；
  源级故障只把「取数」这一步降级（serpDisabled / trendsDisabled），其它词照常出结论。

★trace：整批一条 span（本文件外壳），每个词一条子 span（循环图 analyze_one 节点），
  词里的 ChatOpenAI 再挂在词 span 下。批次 run 经 state 显式传给图。
"""

from __future__ import annotations

import logging
import uuid
import time
from typing import Any, Optional

from src.app.config import settings
from src.app.observability.langsmith import run_traced

from src.app.tools.adapters.cloudflare.kv import CloudflareKV, get_kv
from .hotword_worker import _BatchAbort
from .hotword_contract import (
    _select_pending, BATCH_OK_MIN_RATE, BATCH_TRACE_NAME, WORD_TRACE_PREFIX,
)
from .hotword_loop import HotwordLoopState, summarize

log = logging.getLogger(__name__)


def _hotword_graph():
    """取后台循环图（装配在 graph/builder.py:build_hotword_graph）。

    惰性取图是**加载期边界**，不是兼容补丁：builder 顶层会拉起整条主图依赖链
    （nodes → services → registry → hotwords_tools → 门面 → 本模块），若在本模块顶层
    import builder 会形成加载期环。调用到此函数时 builder 早已加载完。图本身是
    lru_cache 单例，第一次调用才编译。
    """
    from src.app.graph.builder import get_hotword_graph
    return get_hotword_graph()


def _batch_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """批次 span 的 outputs：LangSmith 上直接看成功数与降级情况，不用点开 items。"""
    keys = ("ok", "fetched", "analyzed", "failed", "successRate", "counts",
            "serpDisabled", "trendsDisabled", "llmFailures", "error", "note")
    return {k: out[k] for k in keys if k in out}


def _trace_run_info(run, batch_id: str) -> dict[str, Any]:
    """批次回链信息（附加字段，老前端忽略也不受影响）。

    只拿已配置的 LANGSMITH_PROJECT_URL 拼 URL，**不在这里发网络请求**；没配就留空，
    用 runId 在 LangSmith 搜索框里一样能定位这条根 trace。
    """
    base = settings.langsmith_project_url.strip().rstrip("/")
    return {
        "batchId": batch_id,
        "runId": str(run.id),
        "traceId": str(getattr(run, "trace_id", "") or run.id),
        "url": f"{base}/r/{run.id}?poll=true" if base else "",
    }


def run_batch(limit: int = 20, kv: Optional[CloudflareKV] = None,
              prompt_version: Optional[str] = None) -> dict[str, Any]:
    """读 KV 候选池 → 逐词分析（循环图驱动）→ 汇总。KV 不可达时整批失败，不写状态。

    ★逐词调度交给 graph/builder.py 装配的 StateGraph 回环图（节点逻辑在 hotword_loop.py），
      一次一词、串行推进，limit 即最大循环数。

    ★不再有「整批中止」。每个词独立跑完并把自己的失败写进 items[].error；
      源级故障只把「取数」这一步降级（serpDisabled / trendsDisabled），其它词照常出结论。
      整表写入都在 kv.atomic_update 的进程级 RLock 里完成。

    ★trace：整批一条 span（本函数外壳），每个词一条子 span（循环图 analyze_one 节点），
      词里的 ChatOpenAI 再挂在词 span 下 —— Traces 页面展开一条就能看完 20 个词。
      批次 run 经 state 显式传给图。
    """
    batch_id = f"hotwords-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"

    def body(batch_run):
        out = _run_batch(limit, kv, batch_run, batch_id, prompt_version)
        # 放在包装层：KV 读失败 / 无候选这些提前返回也带同一条根 run 的回链
        if batch_run is not None:
            out["traceRun"] = _trace_run_info(batch_run, batch_id)
        return out

    resolved_version = (prompt_version
                        or (settings.default_prompt_versions or {}).get("hotwords", "v1"))
    return run_traced(
        body,
        name=BATCH_TRACE_NAME,
        inputs={"limit": limit, "batchId": batch_id, "promptVersion": resolved_version},
        tags=["site-agent", "hotword-batch"],
        metadata={"batch_id": batch_id, "thread_id": batch_id,
                  "component": "analyze_hotwords", "prompt_version": resolved_version},
        outputs=_batch_outputs,
        pass_run=True,
    )


def _run_batch(limit: int, kv: Optional[CloudflareKV], batch_run,
               batch_id: str, prompt_version: Optional[str] = None) -> dict[str, Any]:
    """run_batch 的执行体：构造循环图 initial state → invoke → 聚合回对外结构。

    batch_run 是批次 span 的 run，经 state 显式传给图里每个节点（每词 span 的父）。
    逐词循环、limit 封顶、候选/判例读取全部在 hotword_loop.py 的图里完成；
    本函数不写任何 while/for 调度，只做一次 invoke。
    """
    kv = kv or get_kv()
    limit = max(1, min(int(limit or 20), 50))
    resolved_version = (prompt_version
                        or (settings.default_prompt_versions or {}).get("hotwords", "v1"))

    initial: HotwordLoopState = {
        "limit": limit,
        "kv": kv,
        "prompt_version": resolved_version,
        "batch_run": batch_run,
        "batch_id": batch_id,
        "abort": _BatchAbort(),
        "loaded": False,
        "queue": [],
        "cursor": 0,
        "results": [],
        "loop_count": 0,
        "examples": [],
    }
    final = _hotword_graph().invoke(initial)
    return summarize(final)
