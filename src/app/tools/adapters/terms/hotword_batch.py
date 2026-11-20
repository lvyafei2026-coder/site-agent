# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的批次外壳：批次 trace 根 + 对外契约 + 循环图加载。

本文件是热词子系统对外的执行入口（run_batch）。单词内部（补数/LLM/回写）在
hotword_worker.py，取数在 hotword_fetch.py，候选筛选规则与 trace 常量在
共享叶子 hotword_contract.py。

★逐词调度由 graph/builder.py 装配的 StateGraph 回环图承担（fetch_one → analyze_one
  → should_continue 回环）；节点逻辑在 hotword_loop.py。本文件只做三件事：
  构造循环图 initial state → invoke → summarize；外层套批次 trace + traceRun 回链。
  图从 graph/hotword_graph.py 顶层静态 import（叶子装配模块，只依赖 loop，无环）。

★2026-11-13：不再有「整批中止」。每个词独立跑完并把自己的失败写进 items[].error；
  源级故障只把「取数」这一步降级（serpDisabled / trendsDisabled），其它词照常出结论。

★trace：整批一条 span（本文件外壳），每个词一条子 span（循环图 analyze_one 节点），
  词里的 ChatOpenAI 再挂在词 span 下。批次 run 经 state 显式传给图。
"""

from __future__ import annotations

import logging
import uuid
import time
from functools import partial
from typing import Any, Optional

from src.app.config import settings
from src.app.graph.hotword_graph import get_hotword_graph
from src.app.observability.langsmith import run_traced

from src.app.tools.adapters.cloudflare.kv import get_kv
from .hotword_worker import _BatchAbort
from .hotword_contract import BATCH_TRACE_NAME
from .hotword_loop import HotwordLoopState, summarize

log = logging.getLogger(__name__)


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


def _batch_body(batch_run, *, limit: int, batch_id: str,
                prompt_version: Optional[str]) -> dict[str, Any]:
    """批次 span 的执行体（模块级，避免 run_batch 内嵌 def）。

    第一个位置参数 batch_run 由 run_traced(pass_run=True) 注入；其余参数在 run_batch
    里用 functools.partial 绑定。KV 读失败 / 无候选这些提前返回也带同一条根 run 的回链。
    """
    out = _run_batch(limit, batch_run, batch_id, prompt_version)
    if batch_run is not None:
        out["traceRun"] = _trace_run_info(batch_run, batch_id)
    return out


def run_batch(limit: int = 20,
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
    body = partial(_batch_body, limit=limit,
                   batch_id=batch_id, prompt_version=prompt_version)
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


def _run_batch(limit: int, batch_run, batch_id: str,
               prompt_version: Optional[str] = None) -> dict[str, Any]:
    """run_batch 的执行体：构造循环图 initial state → invoke → 聚合回对外结构。

    batch_run 是批次 span 的 run，经 state 显式传给图里每个节点（每词 span 的父）。
    逐词循环、limit 封顶、候选/判例读取全部在 hotword_loop.py 的图里完成；
    本函数不写任何 while/for 调度，只做一次 invoke。
    """
    kv = get_kv()
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
    final = get_hotword_graph().invoke(initial)
    return summarize(final)
