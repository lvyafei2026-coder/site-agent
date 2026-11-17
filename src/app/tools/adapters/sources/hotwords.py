# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析编排层：批量取「未跟进」候选 → 补竞争度/趋势 → LLM 出站点方案 → 直写共享 KV。

2026-11-12 去接口化改造：本模块**不再调用** site-console 的 /api/hotwords 与
google-trend-radar 的 /api/serp、/api/trend、/api/candidates。它直接读写三方共用的共享 KV
（Cloudflare KV REST API，见 ../cloudflare/kv.py），数据链路只有「存储介质依赖」，没有「服务接口依赖」：

  候选池    → 读 KV `candidates`
  未跟进判定 → 读 KV `saved` / `rejected` / `tags` + 列举 `ai:v3:` 前缀
  竞争度/趋势 → 读 KV `serp:v1:<词>` / `trend:v1:<词>`（缺失时由本模块用 scrape 模块
              现抓并写回 KV，同时更新 `competition` / `interests` 摘要表）
  LLM 正文  → 直写 KV `ai:v3:<词>`
  信号标签  → 直写 KV `aihints`
  结论「不做」→ 直写 KV `rejected`（source=ai，人工标记优先）
  结论「做」  → 直写 KV `saved`（status=todo，待开发）
  结论「观望」→ 只留正文与标签，状态仍为未跟进

★本文件按层拆分（2026-11-17，原 1200+ 行）：
    hotword_criteria.py  纯判据：提示词拼装、结论/红线/信号标签解析（无 IO）
    hotword_llm.py       LLM 调用与空正文诊断
    hotword_kv_io.py     KV 写账本（_WriteLog）+ 形状兜底读取
    hotword_backfill.py  状态回填 backfill_states
  下面用 `from .hotword_xxx import ...` 把公开名字原样再导出——tests / runbook /
  api/routes.py 都按 `hotwords.<name>` 引用，门面名不变。

★2026-11-17：发给 LLM 的 system 与 user 模板唯一真源 = radar-rules D1 的 prompts 表
  （由 /rules/prompts 页面维护，本地 versions/*.txt 已删除）。判据层只「拼数据」。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from src.app.config import settings
from src.app.enums import (
    AnalysisPhase, CurationStatus, RejectSource, Verdict,
)
from src.app.observability.langsmith import run_traced, trace_tool, tracing_enabled

from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, get_kv, term_key,
    AI_CACHE_PREFIX, SERP_PREFIX, TREND_PREFIX,
    SAVED_KEY, REJECTED_KEY, TAGS_KEY, COMPETITION_KEY, INTERESTS_KEY, HINTS_KEY,
)
from . import scrape_serp, scrape_trend
from .source_errors import SourceUnavailable
from .curation import upsert_saved, upsert_rejected, set_hint_override

# ---- 判据层（再导出，保持 hotwords.<name> 门面）----
from .hotword_criteria import (  # noqa: F401  (HINT_NAMES/EXAMPLE_LIMIT 供测试与外部按门面引用)
    EXAMPLE_LIMIT, _prompt_pair, pick_examples, examples_line,
    HINT_NAMES, WORK_TITLE_RX, build_messages,
    serp_brief, work_signal, verdict_of, redline_of, signals_of, decide,
)
# ---- LLM 层 ----
from .hotword_llm import (
    MAX_TOKENS, LLMEmptyContent, _field, _thinking_kwargs,
    _empty_content_diag, _llm,
)
# ---- KV 写账本层 ----
from .hotword_kv_io import _WriteLog, _kv_list, _kv_dict  # noqa: F401 (_io_size 留在 kv_io)
# ---- 状态回填（api/routes.py 经 hotwords.backfill_states 引用）----
from .hotword_backfill import backfill_states

log = logging.getLogger(__name__)

BATCH_WORKERS = 4           # 20 条逐条分析，并发压住外部 LLM 超时
BATCH_OK_MIN_RATE = 0.5     # 整批成功判定：成功率低于此值即视为失败（不再「1 条成功就算成功」）
SERP_GAP_SEC = 1.5          # 全局串行 + 最小间隔；202 的根因是请求头，见 scrape_serp.py
SERP_TTL = 604800           # serp:v1: 缓存 7 天
TREND_TTL = 259200          # trend:v1: 缓存 3 天

# ★2026-11-13：LangSmith 一条根 trace 的层级 =
#   graph 根 → 批次 span → 每词 span → [tool:serp / tool:trend / ChatOpenAI / tool:kv.write]。
#   ThreadPoolExecutor 的 worker 线程不继承主线程 contextvars，父 run 必须显式传（见
#   observability/langsmith.py:run_traced）；词 span 拿到自己的 run 后，再把它作为
#   所有 tool span 的 parent，SERP/Trends/KV 这些「原本只存在于日志里」的调用才会
#   出现在 Traces 页面上。graph 结构本身一个字都不改。
BATCH_TRACE_NAME = "site-agent:hotword-batch"
WORD_TRACE_PREFIX = "site-agent:hotword:"
TOOL_TRACE_PREFIX = "tool:"

# ★2026-11-13（用户定）：批次不再因某一个词/某一个源失败而中止——「完成一个结束一个、互不影响」。
#   ・SERP 源整体不可用 → 只让该词跳过 SERP（notice 里标明缺辨形线索），本批后续词也跳过，
#     避免每个词都撞一遍 4 发超时；LLM 照常出结论。
#   ・LLM 连败 → 只记该词 error，不再 trip 停掉整批。
#   ・任何 put_* 失败 → 该词记 error、不算 analyzed；正文已写但状态表丢失的词由
#     backfill_states() 从正文重新补写状态（否则 _select_pending 会永远剔除它）。
#   ABORT_ON_SERP_DOWN 因此删除：不再保留「整批中止」这条分支。

_serp_lock = threading.Lock()
_last_serp_at = 0.0


def _tool_step(name: str, call, *, inputs: dict[str, Any], outputs,
               parent, term: str):
    """逐词内部的一步 IO = 一条 `tool:*` span，显式挂在词 span 下。

    ★不吞异常：SERP/Trends 源不可用时 SDK 把 traceback 写进 span.error 并标红，
      业务侧照常 catch 后降级。这样 Smith 上既看得到「调用过」，也看得到「为什么跳过」。
    """
    return trace_tool(
        call,
        name=TOOL_TRACE_PREFIX + name,
        inputs=inputs,
        parent=parent,                # ★worker 线程：必须显式传词 span 的 run
        tags=["site-agent", "hotword-tool"],
        metadata={"term": term, "component": "analyze_hotwords"},
        outputs=outputs,
    )


# ---------------------------------------------------------------------------
# 外部抓取（SERP / Trends）：读缓存 / 现抓 / 写回 + 全局节流
# ---------------------------------------------------------------------------
def _throttled_serp(term: str, kv: CloudflareKV,
                    writes: Optional[_WriteLog] = None,
                    diag: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any] | None, str]:
    """KV 已有 serp:v1: 直接读；缺失则现抓并写回。全局串行避免并发同时打 DDG。

    ★源级不可用（连不上 / 被挑战页占满）原样抛 SourceUnavailable，由 _analyze_one
      **只让本词跳过 SERP**，不再中止整批；其它异常降级为「无 SERP」，不拦 AI。
    ★diag 是给 LangSmith span 的现场记录（是否命中缓存、4 发尝试明细、抓取异常），
      不改变返回值形状，老调用方不受影响。
    """
    diag = {} if diag is None else diag
    diag["fromCache"] = False
    key = term_key(term)
    writes = writes or _WriteLog(term)
    cached = kv.get_json(SERP_PREFIX + key)
    if cached:
        diag["fromCache"] = True
        return cached, ""
    global _last_serp_at
    with _serp_lock:
        wait = _last_serp_at + SERP_GAP_SEC - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_serp_at = time.monotonic()
    try:
        s = scrape_serp.fetch_serp(term)
    except SourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 单词级问题（解析等）不拦 AI
        log.warning("SERP 失败 %s: %s", term, exc)
        diag["fetchError"] = str(exc)[:300]
        return None, str(exc)[:120]
    diag["engine"] = s.get("engine")
    diag["attempts"] = s.get("attempts") or []
    comp = scrape_serp.score_competition(s["items"], term)
    payload = {"engine": s["engine"], "items": s["items"], "competition": comp, "at": int(time.time() * 1000)}
    # 单词缓存键：各词独立、无 read-modify-write，但仍要检查写返回值
    writes.json(kv, SERP_PREFIX + key, payload, ttl=SERP_TTL)
    # competition 是整表：读-改-写必须走原子更新，否则 4 个 worker 会互相覆盖
    entry = {"score": comp["score"], "level": comp["level"], "at": payload["at"]}
    writes.table(kv, COMPETITION_KEY, lambda m: {**m, key: entry}, reader=_kv_dict)
    return payload, ""


def _trend(term: str, kv: CloudflareKV,
           writes: Optional[_WriteLog] = None,
           diag: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
    """KV 已有 trend:v1: 直接读；缺失则现抓并写回 + 更新 interests 摘要表。

    ★源级不可用会抛 SourceUnavailable，由 _analyze_one 把「本批禁用趋势」置位——趋势只是
      辅助证据，不该因为它连不上就让整批分析白跑，但也不该每个词都去撞一遍超时。
    ★diag 同理记录命中缓存 / 抓取点数 / 尝试明细，供 tool:trend span 输出。
    """
    diag = {} if diag is None else diag
    diag["fromCache"] = False
    key = term_key(term)
    writes = writes or _WriteLog(term)
    cached = kv.get_json(TREND_PREFIX + key)
    if cached:
        diag["fromCache"] = True
        diag["pointCount"] = len(cached.get("points") or [])
        return True, ""
    try:
        t = scrape_trend.fetch_interest(term)
    except SourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 单词解析问题不影响结论
        log.warning("TREND 失败 %s: %s", term, exc)
        diag["fetchError"] = str(exc)[:300]
        return False, str(exc)[:120]
    diag["attempts"] = t.get("attempts") or []
    diag["pointCount"] = len(t.get("points") or [])
    diag["summary"] = t.get("summary") or {}
    payload = {"points": t["points"], "summary": t["summary"], "at": int(time.time() * 1000)}
    writes.json(kv, TREND_PREFIX + key, payload, ttl=TREND_TTL)
    entry = {
        "term": term, "summary": t["summary"], "at": payload["at"],
        "points": [p["v"] for p in t["points"]],
    }
    writes.table(kv, INTERESTS_KEY, lambda m: {**m, key: entry}, reader=_kv_dict)
    return True, ""


# ---------------------------------------------------------------------------
# 编排：一次 20 条，逐条走完「补数据 → 分析 → 回写状态」；全部读写共享 KV
# ---------------------------------------------------------------------------
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


class _BatchAbort:
    """批次级**降级状态**（名字沿用历史；2026-11-13 起不再有「中止整批」能力）。

    单词级失败隔离后，这里只保留三件事，全都是「省时间 / 供汇总」，不再拦任何词：

      ・SERP 源不可用   → serp_off：本批后续词跳过 SERP，省掉每词 4 发超时；
                           当前词与后续词都照常走 LLM、照常回写状态（notice 标缺线索）。
      ・Trends 源不可用 → trends_off：同上，趋势只是辅助证据。
      ・LLM 失败        → 只累加次数供汇总，**不 trip、不中断**；该词的 error 由调用方记。

    为什么仍保留批次级开关：源级故障是「出口 IP 不通」这类全局问题，让 20 个词各撞
    一遍 6 秒超时毫无意义；跳过的是「取数」这一步，不是整词分析。
    """

    def __init__(self) -> None:
        self.trends_off = False
        self.serp_off = False
        self.llm_failures = 0
        self._lock = threading.Lock()

    def disable_trends(self) -> bool:
        """首次调用返回 True（用于只打一条日志）；之后恒 False。"""
        with self._lock:
            first = not self.trends_off
            self.trends_off = True
            return first

    def disable_serp(self) -> bool:
        """本批后续词不再尝试 SERP；返回是否首次置位。"""
        with self._lock:
            first = not self.serp_off
            self.serp_off = True
            return first

    def note_llm_failure(self) -> int:
        """记一次 LLM 失败，返回累计次数。只用于日志/汇总，不再据此中止整批。"""
        with self._lock:
            self.llm_failures += 1
            return self.llm_failures


def _finalize(out: dict[str, Any], writes: _WriteLog) -> dict[str, Any]:
    """把本词所有写入失败汇总进 error —— 有任何一个写失败就不算 analyzed。"""
    if writes.failed:
        out["writeErrors"] = list(writes.failed)
        out["error"] = (out["error"] + "；" if out["error"] else "") + writes.error()
    return out


def _analyze_one(item: dict[str, Any], kv: CloudflareKV,
                 state: Optional[_BatchAbort] = None,
                 parent_run=None,
                 prompt_version: Optional[str] = None,
                 examples: Optional[list[dict[str, str]]] = None) -> dict[str, Any]:
    """一个词走完「补数据 → LLM → 回写状态」，失败只落在这个词自己身上。

    ★不抛异常给上层：SERP/Trends/LLM/KV 任一环出问题都写进 items[].error，
      其它词继续跑（完成一个结束一个、互不影响）。
    ★parent_run 是本词 span 的 RunTree：SERP / Trends / KV 三处 IO 各自建一条
      `tool:*` 子 span 并显式挂在它下面（worker 线程没有主线程 contextvars）。
      追踪关闭时它是 None，`trace_tool` 直接调函数、零开销。
    """
    term = str(item.get("term") or "").strip()
    out: dict[str, Any] = {"term": term, "verdict": "", "status": "", "model": "",
                           "serp": False, "trend": False, "error": ""}
    if not term:
        out["error"] = "空词"
        return out

    writes = _WriteLog(term)
    writes.capture = tracing_enabled()
    key = term_key(term)

    # ---- SERP：读缓存 / 现抓 / 写回，整段是一条 tool:serp span ----
    serp_err = ""
    serp_diag: dict[str, Any] = {}
    writes.phase = AnalysisPhase.SERP.value
    serp_mark = len(writes.records)

    def serp_outputs(res):
        data, err = res
        return {
            "engine": (data or {}).get("engine") or serp_diag.get("engine") or "",
            "itemCount": len((data or {}).get("items") or []),
            "items": (data or {}).get("items") or [],
            "competition": (data or {}).get("competition") or {},
            "fromCache": bool(serp_diag.get("fromCache")),
            "attempts": serp_diag.get("attempts") or [],
            "fetchError": serp_diag.get("fetchError") or "",
            "error": err,
            "kvWrites": writes.records[serp_mark:],
        }

    if state is not None and state.serp_off:
        serp_data, serp_err = None, "本批已跳过 SERP（源不可用）"
        # 「跳过」也要留痕：否则 Smith 上这一词像是压根没打算取 SERP
        _tool_step(
            "serp", lambda: (None, serp_err),
            inputs={"term": term, "key": key, "skipped": True,
                    "reason": serp_err},
            outputs=lambda res: {"skipped": True, "reason": res[1],
                                 "kvWrites": []},
            parent=parent_run, term=term,
        )
    else:
        try:
            serp_data, serp_err = _tool_step(
                "serp", lambda: _throttled_serp(term, kv, writes, serp_diag),
                inputs={"term": term, "key": key, "cacheKey": SERP_PREFIX + key},
                outputs=serp_outputs, parent=parent_run, term=term,
            )
        except SourceUnavailable as exc:
            # span 已被 SDK 标红（含完整 traceback）；这里只做业务降级。
            log.warning("SERP 源不可用，本词跳过 SERP 继续分析：%s", exc.brief())
            serp_data, serp_err = None, exc.brief()
            if state is not None and state.disable_serp():
                log.warning("本批后续词跳过 SERP（结论缺少辨形线索）")
    out["serp"] = serp_data is not None

    # ---- Trends：读缓存 / 现抓 / 写回，整段是一条 tool:trend span ----
    trend_ok, trend_err = False, ""
    trend_diag: dict[str, Any] = {}
    writes.phase = AnalysisPhase.TREND.value
    trend_mark = len(writes.records)

    def trend_outputs(res):
        ok, err = res
        return {
            "ok": ok, "fromCache": bool(trend_diag.get("fromCache")),
            "pointCount": trend_diag.get("pointCount") or 0,
            "summary": trend_diag.get("summary") or {},
            "attempts": trend_diag.get("attempts") or [],
            "fetchError": trend_diag.get("fetchError") or "",
            "error": err,
            "kvWrites": writes.records[trend_mark:],
        }

    if state is not None and state.trends_off:
        trend_err = "本批已禁用趋势（源不可用）"
        _tool_step(
            "trend", lambda: (False, trend_err),
            inputs={"term": term, "key": key, "skipped": True,
                    "reason": trend_err},
            outputs=lambda res: {"skipped": True, "reason": res[1],
                                 "kvWrites": []},
            parent=parent_run, term=term,
        )
    else:
        try:
            trend_ok, trend_err = _tool_step(
                "trend", lambda: _trend(term, kv, writes, trend_diag),
                inputs={"term": term, "key": key, "cacheKey": TREND_PREFIX + key},
                outputs=trend_outputs, parent=parent_run, term=term,
            )
        except SourceUnavailable as exc:
            trend_err = exc.brief()
            if state is not None and state.disable_trends():
                log.warning("本批禁用趋势：%s", exc.brief())
    out["trend"] = trend_ok

    # 补数据缺一块不等于这条分析失败（AI 已经给出结论了），只记 notice 供前端提示。
    notes = []
    if serp_err:
        notes.append("SERP 未取到：" + serp_err)
    if trend_err:
        notes.append("趋势未取到：" + trend_err)
    if notes:
        out["notice"] = "；".join(notes)

    brief = serp_brief(serp_data)
    signal = work_signal(brief)
    try:
        text, model = _llm(build_messages(dict(item, serp=brief),
                                          prompt_version=prompt_version,
                                          examples=examples))
    except LLMEmptyContent as exc:
        # 「调用成功但没正文」≠「调用异常」：错误说明必须来自真实诊断，
        # 不能笼统写成「LLM_API_KEY 未配置」，也不影响其它词。
        out["error"] = str(exc)
        out["llm_diag"] = exc.diag
        return _finalize(out, writes)
    except Exception as exc:  # noqa: BLE001
        out["error"] = "LLM 调用失败：" + str(exc)[:160]
        if state is not None:
            n = state.note_llm_failure()
            log.warning("LLM 失败（本批第 %d 次，继续跑其它词）：%s", n, str(exc)[:120])
        return _finalize(out, writes)
    if not text:
        # 兜底：_llm 正常会抛 LLMEmptyContent；若某个注入实现仍返回空串，
        # 也绝不把空正文写进 KV，且不臆断成「key 没配」。
        out["error"] = "LLM 返回空正文（未携带诊断信息，请查 hotwords_llm_empty_content 日志）"
        return _finalize(out, writes)
    out["model"] = model

    verdict, reason = decide(text, signal)
    out["verdict"] = verdict
    hints = signals_of(text)

    # ---- 回写 KV：正文 / 标签 / 状态，整段是一条 tool:kv.write span ----
    # ★正文先写：正文写失败就直接收手，绝不写状态——否则会留下「有状态、没正文」的悬空词，
    #   _select_pending 既不会重跑它，前端也看不到方案。
    writes.phase = AnalysisPhase.ANALYSIS.value
    write_mark = len(writes.records)
    stopped = {"afterTextFailure": False}

    def write_back():
        if not writes.text(kv, AI_CACHE_PREFIX + key, text):
            stopped["afterTextFailure"] = True
            return
        if hints is not None:
            writes.table(kv, HINTS_KEY,
                         lambda m: set_hint_override(m, term, hints), reader=_kv_dict)
        if verdict == Verdict.SKIP.value:
            payload = {"term": term, "reason": reason or "AI 判定不做",
                       "source": RejectSource.AI.value}
            if writes.table(kv, REJECTED_KEY,
                            lambda arr: upsert_rejected(arr, payload), reader=_kv_list):
                out["status"] = "rejected"
        elif verdict == Verdict.DO.value:
            cand = {k: item.get(k) for k in ("term", "related", "hints", "geos", "geoCount", "traffic", "count", "score")}
            payload = {**cand, "term": term, "status": CurationStatus.TODO.value,
                       "verdict": Verdict.DO.value}
            if writes.table(kv, SAVED_KEY,
                            lambda arr: upsert_saved(arr, payload), reader=_kv_list):
                out["status"] = CurationStatus.TODO.value
        # 「观望」与解析不出结论：只留正文和标签，状态保持未跟进

    def write_outputs(_result):
        return {
            "verdict": verdict, "status": out.get("status") or "",
            "textChars": len(text), "textPreview": text[:200],
            "hints": hints,
            "stoppedAfterTextFailure": stopped["afterTextFailure"],
            "writes": writes.records[write_mark:],
            "writeErrors": list(writes.failed),
        }

    _tool_step(
        "kv.write", write_back,
        inputs={"term": term, "key": key, "aiCacheKey": AI_CACHE_PREFIX + key,
                "verdict": verdict, "hints": hints,
                "tables": [HINTS_KEY, SAVED_KEY, REJECTED_KEY]},
        outputs=write_outputs, parent=parent_run, term=term,
    )

    return _finalize(out, writes)


# ---------------------------------------------------------------------------
# 批次 trace：graph 根 → 批次 span → 每词 span → ChatOpenAI（graph 结构本身不动）
# ---------------------------------------------------------------------------
def _batch_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """批次 span 的 outputs：LangSmith 上直接看成功数与降级情况，不用点开 items。"""
    keys = ("ok", "fetched", "analyzed", "failed", "successRate", "counts",
            "serpDisabled", "trendsDisabled", "llmFailures", "error", "note")
    return {k: out[k] for k in keys if k in out}


def _word_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """逐词 span 的 outputs：结论、状态、补数情况与写入失败明细。"""
    keys = ("verdict", "status", "model", "serp", "trend", "notice",
            "writeErrors", "error", "llm_diag")
    return {k: out[k] for k in keys if k in out}


def _word_error(out: dict[str, Any]) -> str | None:
    """业务失败也让 LangSmith 标红：_analyze_one 不抛异常，trace 自己看不到失败。"""
    return out.get("error") or None


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
