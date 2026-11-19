# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的批次入口：候选筛选规则 + 批次 trace 根 + 对外契约。

拆分自 hotwords.py（2026-11-19 三层拆分）。单词内部（补数/LLM/回写）在
hotword_worker.py，取数在 hotword_fetch.py。

★2026-11-19 循环图改造：逐词调度**不再是本文件的 ThreadPoolExecutor 并发**，
  而是交给独立的 hotword_loop.py——一张 StateGraph 条件边回环图（fetch_one →
  analyze_one → should_continue 回环）。本文件只保留：候选筛选规则 _select_pending、
  对外 run_batch 外壳（批次 trace + traceRun 回链），执行体转交循环图。

★2026-11-13：不再有「整批中止」。每个词独立跑完并把自己的失败写进 items[].error；
  源级故障只把「取数」这一步降级（serpDisabled / trendsDisabled），其它词照常出结论。

★trace：整批一条 span（run_batch 外壳），每个词一条子 span（循环图的 analyze_one
  节点里创建），词里的 ChatOpenAI 再挂在词 span 下。批次 run 经 state 显式传给图。
"""

from __future__ import annotations

import logging
import uuid
import time
from typing import Any, Optional

from src.app.config import settings
from src.app.observability.langsmith import run_traced

from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, get_kv, term_key,
    AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from .hotword_kv_io import _kv_list, _kv_dict
from .hotword_worker import _BatchAbort, TOOL_TRACE_PREFIX
from .hotword_loop import HotwordLoopState, get_hotword_loop, summarize

log = logging.getLogger(__name__)

# 保留为契约常量：循环图改串行，门面/测试仍按 hw.BATCH_WORKERS 取「本批最大在飞」语义，
# 现在它只表示「一次循环图的并发度=1 的上界」，值不变以免测试断言（calls<=4）失效。
BATCH_WORKERS = 4
BATCH_OK_MIN_RATE = 0.5     # 整批成功判定：成功率低于此值即视为失败（不再「1 条成功就算成功」）

BATCH_TRACE_NAME = "site-agent:hotword-batch"
WORD_TRACE_PREFIX = "site-agent:hotword:"


# ★2026-11-13：LangSmith 一条根 trace 的层级 =
#   graph 根 → 批次 span → 每词 span → [tool:serp / tool:trend / ChatOpenAI / tool:kv.write]。
#   ThreadPoolExecutor 的 worker 线程不继承主线程 contextvars，父 run 必须显式传（见
#   observability/langsmith.py:run_traced）；词 span 拿到自己的 run 后，再把它作为
#   所有 tool span 的 parent，SERP/Trends/KV 这些「原本只存在于日志里」的调用才会
#   出现在 Traces 页面上。graph 结构本身一个字都不改。


def _select_pending(kv: CloudflareKV, limit: int) -> list[dict[str, Any]]:
    """读 KV 候选池，剔除已跟进/打标签/已分析的词，返回前 limit 条未跟进候选。

    ★形状兜底：KV 里这几张表是 console/radar 写的，字段形状一旦漂移（比如 tags 变成数组、
    candidates 变成对象），按原样 .items() / 迭代 会整批失败。这里统一按「期望类型不符就
    当空」处理并 warn，让漂移可见但不阻断分析。
    """
    candidates = _kv_list(kv, "candidates")
    saved = _kv_list(kv, SAVED_KEY)
    rejected = _kv_list(kv, REJECTED_KEY)
    tags = _kv_dict(kv, TAGS_KEY)

    curated = set()
    for x in (saved + rejected):
        if isinstance(x, dict) and x.get("key"):
            curated.add(term_key(x["key"]))
    for k, v in tags.items():
        if v:
            curated.add(term_key(k))

    analyzed = {k[len(AI_CACHE_PREFIX):] for k in kv.list_keys(AI_CACHE_PREFIX)}

    items: list[dict[str, Any]] = []
    for c in candidates:
        if not isinstance(c, dict) or not c.get("term"):
            continue
        key = term_key(c.get("key") or c.get("term"))
        if key in curated or key in analyzed:
            continue
        items.append({
            "key": key,
            "term": str(c.get("term") or "")[:120],
            "related": [str(x) for x in (c.get("related") or [])][:6],
            "hints": [str(x) for x in (c.get("hints") or [])][:6],
            "geos": [str(x) for x in (c.get("geos") or [])][:16],
            "geoCount": int(c.get("geoCount") or (len(c.get("geos") or []))),
            "traffic": int(c.get("traffic") or 0),
            "count": int(c.get("count") or 1),
            "score": int(c.get("score") or 0),
            "src": str(c.get("src") or "trends"),
        })
        if len(items) >= limit:
            break
    return items


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

    ★2026-11-19：逐词调度交给 hotword_loop.py 的独立 StateGraph 条件边回环图，
      一次一词、串行推进，limit 即最大循环数；不再有 ThreadPoolExecutor 并发分块。

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
        "current_index": 0,
        "examples": [],
    }
    final = get_hotword_loop().invoke(initial)
    return summarize(final)
