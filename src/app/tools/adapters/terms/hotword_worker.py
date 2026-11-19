# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的单词编排层：一个词走完「补数据 → LLM → 结论解析 → 回写状态」。

拆分自 hotwords.py（2026-11-19 三层拆分）。本层把取数（hotword_fetch）、判据
（hotword_criteria）、LLM（hotword_llm）、写账本（hotword_kv_io）、整理写侧
（curation）拼成一个词的分析；批次并发与 trace 根在 hotword_batch.py。

★不抛异常给上层：SERP/Trends/LLM/KV 任一环出问题都写进 items[].error，
  其它词继续跑（完成一个结束一个、互不影响）。
★parent_run 是本词 span 的 RunTree：SERP / Trends / KV 三处 IO 各自建一条
  `tool:*` 子 span 并显式挂在它下面（worker 线程没有主线程 contextvars）。
  追踪关闭时它是 None，`trace_tool` 直接调函数、零开销。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from src.app.enums import (
    AnalysisPhase, CurationStatus, RejectSource, Verdict,
)
from src.app.observability.langsmith import trace_tool, tracing_enabled

from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, term_key,
    AI_CACHE_PREFIX, SERP_PREFIX, TREND_PREFIX,
    SAVED_KEY, REJECTED_KEY, HINTS_KEY,
)
from .hotword_fetch import _throttled_serp, _trend
from .hotword_criteria import (
    serp_brief, work_signal, build_messages, decide, signals_of,
)
from .hotword_llm import LLMEmptyContent
from .hotword_kv_io import _WriteLog, _kv_list, _kv_dict
from .curation import upsert_saved, upsert_rejected, set_hint_override
from .source_errors import SourceUnavailable

log = logging.getLogger(__name__)

TOOL_TRACE_PREFIX = "tool:"


def _resolve_llm():
    """运行时从门面取 `_llm` —— 测试 `monkeypatch.setattr(hw, "_llm", ...)` 才能生效。

    延迟 import 避开 hotwords → worker → hotwords 的加载期循环（调用时门面已加载完）；
    adapters 子目录的延迟 import 被 test_tools_have_no_deferred_imports 明确豁免。
    """
    from . import hotwords as facade
    return facade._llm


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
        text, model = _resolve_llm()(build_messages(dict(item, serp=brief),
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


def _word_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """逐词 span 的 outputs：结论、状态、补数情况与写入失败明细。"""
    keys = ("verdict", "status", "model", "serp", "trend", "notice",
            "writeErrors", "error", "llm_diag")
    return {k: out[k] for k in keys if k in out}


def _word_error(out: dict[str, Any]) -> str | None:
    """业务失败也让 LangSmith 标红：_analyze_one 不抛异常，trace 自己看不到失败。"""
    return out.get("error") or None
