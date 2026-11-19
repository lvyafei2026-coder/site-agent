# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的批次编排层：候选筛选 → 并发逐词分析 → 汇总 + 批次 trace 根。

拆分自 hotwords.py（2026-11-19 三层拆分）。本层只管「一批怎么调度」，单词内部
（补数/LLM/回写）在 hotword_worker.py，取数在 hotword_fetch.py。graph 结构一个字不动。

★2026-11-13：不再有「整批中止」。每个词独立跑完并把自己的失败写进 items[].error；
  源级故障只把「取数」这一步降级（serpDisabled / trendsDisabled），其它词照常出结论。
  整表写入都在 kv.atomic_update 的进程级 RLock 里完成，并发不会互相覆盖。

★trace：整批一条 span（自动挂到 LangGraph 根下），每个词一条子 span，词里的
  ChatOpenAI 再挂在词 span 下 —— Traces 页面展开一条就能看完 20 个词，而不是
  散落 20 条孤立根 run。父 run 显式传，绕开 worker 线程的 contextvars 丢失。
"""

from __future__ import annotations

import logging
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from src.app.config import settings
from src.app.enums import Verdict
from src.app.observability.langsmith import run_traced, trace_tool

from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, get_kv, term_key,
    AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from .hotword_kv_io import _kv_list, _kv_dict
from .hotword_criteria import pick_examples
from .hotword_worker import (
    _analyze_one, _BatchAbort, _word_outputs, _word_error, TOOL_TRACE_PREFIX,
)

log = logging.getLogger(__name__)

BATCH_WORKERS = 4           # 20 条逐条分析，并发压住外部 LLM 超时
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
    """读 KV 候选池 → 并发逐条分析 → 汇总。KV 不可达时整批失败，不写任何状态。

    ★2026-11-13：不再有「整批中止」。每个词独立跑完并把自己的失败写进 items[].error；
      源级故障只把「取数」这一步降级（serpDisabled / trendsDisabled），其它词照常出结论。
      整表写入都在 kv.atomic_update 的进程级 RLock 里完成，并发不会互相覆盖。

    ★trace：整批一条 span（自动挂到 LangGraph 根下），每个词一条子 span，词里的
      ChatOpenAI 再挂在词 span 下 —— Traces 页面展开一条就能看完 20 个词，而不是
      散落 20 条孤立根 run。父 run 显式传，绕开 worker 线程的 contextvars 丢失。
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
    """run_batch 的执行体；batch_run 是逐词 span 的显式父 run（关闭追踪时为 None）。"""
    kv = kv or get_kv()
    limit = max(1, min(int(limit or 20), 50))
    try:
        # 候选池读取也是一次 IO：KV 挂了/形状漂移会在 tool:kv.read-candidates 上标红，
        # 正常时能看到「筛出哪几个词」，不用再去翻日志猜为什么这批跑空了。
        items = trace_tool(
            lambda: _select_pending(kv, limit),
            name=TOOL_TRACE_PREFIX + "kv.read-candidates",
            parent=batch_run,
            inputs={"limit": limit,
                    "keys": ["candidates", SAVED_KEY, REJECTED_KEY, TAGS_KEY,
                             AI_CACHE_PREFIX]},
            tags=["site-agent", "hotword-tool"],
            metadata={"batch_id": batch_id, "component": "analyze_hotwords"},
            outputs=lambda res: {
                "selected": len(res),
                "terms": [x.get("term", "") for x in res],
                "keys": [x.get("key", "") for x in res],
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("候选池读取失败：%s", exc)
        return {"ok": False, "error": "候选池读取失败：" + str(exc)[:200],
                "items": [], "fetched": 0, "analyzed": 0, "failed": 0, "counts": {}}

    if not items:
        return {"ok": True, "items": [], "fetched": 0, "analyzed": 0,
                "failed": 0, "counts": {}, "note": "没有未跟进且未分析过的候选词"}

    # 判例反馈：整批只读一次 rejected，筛出人工判例喂给每个词。
    # ★失败只降级掉判例（examples=[]），不让整批分析因为这张表而挂掉。
    examples: list[dict[str, str]] = []
    try:
        rejected_rows = trace_tool(
            lambda: _kv_list(kv, REJECTED_KEY),
            name=TOOL_TRACE_PREFIX + "kv.read-examples",
            parent=batch_run,
            inputs={"key": REJECTED_KEY},
            tags=["site-agent", "hotword-tool"],
            metadata={"batch_id": batch_id, "component": "analyze_hotwords"},
            outputs=lambda res: {"rejected": len(res) if isinstance(res, list) else 0},
        )
        examples = pick_examples(rejected_rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("判例读取失败，本批不带判例继续：%s", str(exc)[:120])

    state = _BatchAbort()

    def work(pair: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        idx, item = pair
        term = str(item.get("term") or "").strip()
        key = term_key(item.get("key") or term)
        return run_traced(
            # ★pass_run：把本词 span 的 run 交给 _analyze_one，让它成为该词所有
            #   tool:* 子 span 的显式父（worker 线程拿不到主线程的 contextvar）。
            lambda word_run: _analyze_one(item, kv, state, word_run, prompt_version, examples),
            name=WORD_TRACE_PREFIX + (term or key)[:60],
            parent=batch_run,          # ★显式父：worker 线程没有主线程的 contextvar
            inputs={"term": term, "key": key, "index": idx},
            tags=["site-agent", "hotword"],
            metadata={
                "batch_id": batch_id,
                # 整批共用一个 thread_id：Threads 视图也聚成一条，不把 20 个词拆散。
                "thread_id": batch_id,
                # 每词独立标识；要单独看某个词，按 word_id 检索即可。
                "word_id": f"{batch_id}/{idx}/{key}",
                "term": term, "key": key, "index": idx,
            },
            outputs=_word_outputs,
            error=_word_error,
            pass_run=True,
        )

    results: list[dict[str, Any]] = []
    # 按 chunk 提交：一次最多 BATCH_WORKERS 条在飞，外部 LLM/SERP 超时不会把 20 条全排进队列。
    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        for i in range(0, len(items), BATCH_WORKERS):
            chunk = list(enumerate(items[i:i + BATCH_WORKERS], start=i))
            results.extend(pool.map(work, chunk))

    counts: dict[str, int] = {}
    for r in results:
        key = r["verdict"] or Verdict.UNDECIDED.value
        counts[key] = counts.get(key, 0) + 1
    analyzed = sum(1 for r in results if not r["error"])
    failed = len(results) - analyzed
    # 整批口径按成功率判定，并把比例一并回传，前端可直接展示。
    rate = analyzed / len(results) if results else 0.0
    out = {
        "ok": rate >= BATCH_OK_MIN_RATE,
        "fetched": len(items),
        "analyzed": analyzed,
        "failed": failed,
        "successRate": round(rate, 3),
        "counts": counts,
        "items": results,
        "serpDisabled": state.serp_off,
        "trendsDisabled": state.trends_off,
        "llmFailures": state.llm_failures,
    }
    degraded = []
    if state.serp_off:
        degraded.append("SERP 源不可用，本批已跳过 SERP（结论缺少辨形线索）")
    if state.trends_off:
        degraded.append("趋势源不可用，本批已禁用趋势")
    if degraded:
        out["note"] = "；".join(degraded) + "；其余词仍照常分析并回写"
    return out
