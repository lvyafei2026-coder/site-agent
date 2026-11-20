# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的单词编排层：一个词走完「补数据 → LLM → 结论解析 → 回写状态」。

拆自原 adapters/terms/hotword_worker.py（2026-11-20 分层归位）。本层把取数
（hotword_fetch）、判据（hotword_criteria）、LLM（hotword_llm）、写账本（hotword_kv_io）、
整理写侧（curation）拼成一个词的分析——这些 IO 实现都在 tools/adapters/terms 叶子层，
本层是 services，单向依赖它们。

★不抛异常给上层：SERP/Trends/LLM/KV 任一环出问题都写进 items[].error，其它词继续跑。
★trace 是声明式切面：_analyze_one / 取数 / KV 写回各在函数定义处用 @observed 声明 span，
  父子靠 contextvar 自动继承——函数体不写 trace 代码、不传 parent、不拼 outputs 闭包。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.app.enums import (
    AnalysisPhase, CurationStatus, RejectSource, Verdict,
)
from src.app.observability.langsmith import note, observed

from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, term_key,
    AI_CACHE_PREFIX,
    SAVED_KEY, REJECTED_KEY, HINTS_KEY,
)
from src.app.tools.adapters.terms.hotword_fetch import _throttled_serp, _trend
from src.app.tools.adapters.terms.hotword_criteria import (
    serp_brief, work_signal, build_messages, decide, signals_of,
)
from src.app.tools.adapters.terms import hotword_llm
from src.app.tools.adapters.terms.hotword_llm import LLMEmptyContent
from src.app.tools.adapters.terms.hotword_kv_io import _WriteLog, _kv_list, _kv_dict
from src.app.tools.adapters.terms.curation import upsert_saved, upsert_rejected, set_hint_override
from src.app.tools.adapters.terms.source_errors import SourceUnavailable

log = logging.getLogger(__name__)


class _DegradeState:
    """整轮**降级状态**（旧名 _BatchAbort，2026-11-20 去掉 batch 概念）。

    单词级失败隔离后只保留三件事，全是「省时间 / 供汇总」，不拦任何词：

      ・SERP 源不可用 → serp_off：后续词跳过 SERP（省掉每词 4 发超时），照常走 LLM；
      ・Trends 不可用 → trends_off：同上，趋势只是辅助证据；
      ・LLM 失败      → 只累加次数供汇总，不中止。

    源级故障是「出口 IP 不通」这类全局问题，让 20 个词各撞一遍超时毫无意义；
    跳过的是「取数」这一步，不是整词分析。

    热词图逐词串行（无常驻线程），状态只在本轮内单向推进，无需加锁。
    """

    def __init__(self) -> None:
        self.trends_off = False
        self.serp_off = False
        self.llm_failures = 0

    def disable_trends(self) -> bool:
        """首次调用返回 True（用于只打一条日志）；之后恒 False。"""
        first = not self.trends_off
        self.trends_off = True
        return first

    def disable_serp(self) -> bool:
        """后续词不再尝试 SERP；返回是否首次置位。"""
        first = not self.serp_off
        self.serp_off = True
        return first

    def note_llm_failure(self) -> int:
        """记一次 LLM 失败，返回累计次数。只用于日志/汇总。"""
        self.llm_failures += 1
        return self.llm_failures


def _finalize(out: dict[str, Any], writes: _WriteLog) -> dict[str, Any]:
    """把本词所有写入失败汇总进 error —— 有任何一个写失败就不算 analyzed。"""
    if writes.failed:
        out["writeErrors"] = list(writes.failed)
        out["error"] = (out["error"] + "；" if out["error"] else "") + writes.error()
    return out


# ---- 降级留痕：源不可用时后续词跳过取数，也要在 Smith 上看到一条 skipped span ----
@observed("tool:serp", kind="tool",
          inputs=lambda term, reason: {"term": term, "skipped": True, "reason": reason},
          outputs=lambda res: {"skipped": True, "reason": res[1], "kvWrites": []})
def _skip_serp(term: str, reason: str) -> tuple[None, str]:
    return None, reason


@observed("tool:trend", kind="tool",
          inputs=lambda term, reason: {"term": term, "skipped": True, "reason": reason},
          outputs=lambda res: {"skipped": True, "reason": res[1], "kvWrites": []})
def _skip_trend(term: str, reason: str) -> tuple[bool, str]:
    return False, reason


def _write_inputs(item: dict, kv: CloudflareKV, text: str, hints, verdict: str,
                  reason: str, writes: _WriteLog, out: dict) -> dict:
    key = term_key(item.get("key") or item.get("term") or "")
    return {
        "term": out.get("term") or "", "key": key, "aiCacheKey": AI_CACHE_PREFIX + key,
        "verdict": verdict, "hints": hints,
        "tables": [HINTS_KEY, SAVED_KEY, REJECTED_KEY],
    }


@observed("tool:kv.write", kind="tool", inputs=_write_inputs)
def _write_result(item: dict, kv: CloudflareKV, text: str, hints, verdict: str,
                  reason: str, writes: _WriteLog, out: dict) -> None:
    """把本词结论写回 KV：正文 / 标签 / 状态。整段是一条 tool:kv.write span。

    ★正文先写：正文写失败就收手，绝不写状态——否则留下「有状态、没正文」的悬空词，
      select_pending 既不会重跑它，前端也看不到方案。
    ★诊断明细用 note() 挂到当前 span，不向调用方回传、不拼 outputs 闭包。
    """
    writes.phase = AnalysisPhase.ANALYSIS.value
    mark = len(writes.records)
    stopped = {"afterTextFailure": False}

    if not writes.text(kv, AI_CACHE_PREFIX + term_key(item.get("key") or item.get("term") or ""), text):
        stopped["afterTextFailure"] = True
    else:
        if hints is not None:
            writes.table(kv, HINTS_KEY,
                         lambda m: set_hint_override(m, out["term"], hints), reader=_kv_dict)
        if verdict == Verdict.SKIP.value:
            payload = {"term": out["term"], "reason": reason or "AI 判定不做",
                       "source": RejectSource.AI.value}
            if writes.table(kv, REJECTED_KEY,
                            lambda arr: upsert_rejected(arr, payload), reader=_kv_list):
                out["status"] = "rejected"
        elif verdict == Verdict.DO.value:
            cand = {k: item.get(k) for k in ("term", "related", "hints", "geos", "geoCount", "traffic", "count", "score")}
            payload = {**cand, "term": out["term"], "status": CurationStatus.TODO.value,
                       "verdict": Verdict.DO.value}
            if writes.table(kv, SAVED_KEY,
                            lambda arr: upsert_saved(arr, payload), reader=_kv_list):
                out["status"] = CurationStatus.TODO.value
        # 「观望」与解析不出结论：只留正文和标签，状态保持未跟进

    note(verdict=verdict, status=out.get("status") or "", textChars=len(text),
         textPreview=text[:200], hints=hints,
         stoppedAfterTextFailure=stopped["afterTextFailure"],
         writes=writes.records[mark:], writeErrors=list(writes.failed))


def _word_span_name(item: dict, *args, **kwargs) -> str:
    return "site-agent:hotword:" + str(item.get("term") or item.get("key") or "")[:60]


def _word_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """逐词 span 的 outputs：结论、状态、补数情况与写入失败明细。"""
    keys = ("verdict", "status", "model", "serp", "trend", "notice",
            "writeErrors", "error", "llm_diag")
    return {k: out[k] for k in keys if k in out}


def _word_error(out: dict[str, Any]) -> str | None:
    """业务失败也让 span 标红：_analyze_one 不抛异常，trace 自己看不到失败。"""
    return out.get("error") or None


@observed(_word_span_name, kind="chain", outputs=_word_outputs, error=_word_error)
def _analyze_one(item: dict[str, Any], kv: CloudflareKV,
                 state: Optional[_DegradeState] = None,
                 prompt_version: Optional[str] = None,
                 examples: Optional[list[dict[str, str]]] = None) -> dict[str, Any]:
    """一个词走完「补数据 → LLM → 回写状态」，失败只落在这个词自己身上。"""
    term = str(item.get("term") or "").strip()
    out: dict[str, Any] = {"term": term, "verdict": "", "status": "", "model": "",
                           "serp": False, "trend": False, "error": ""}
    if not term:
        out["error"] = "空词"
        return out

    writes = _WriteLog(term)

    # ---- SERP：读缓存 / 现抓 / 写回（tool:serp span 在 _throttled_serp 上）----
    serp_data: dict | None = None
    serp_err = ""
    if state is not None and state.serp_off:
        _, serp_err = _skip_serp(term, "本轮已跳过 SERP（源不可用）")
    else:
        try:
            serp_data, serp_err = _throttled_serp(term, kv, writes)
        except SourceUnavailable as exc:
            log.warning("SERP 源不可用，本词跳过 SERP 继续分析：%s", exc.brief())
            serp_err = exc.brief()
            if state is not None and state.disable_serp():
                log.warning("后续词跳过 SERP（结论缺少辨形线索）")
    out["serp"] = serp_data is not None

    # ---- Trends：读缓存 / 现抓 / 写回（tool:trend span 在 _trend 上）----
    trend_ok = False
    if state is not None and state.trends_off:
        _, trend_err = _skip_trend(term, "本轮已禁用趋势（源不可用）")
    else:
        try:
            trend_ok, trend_err = _trend(term, kv, writes)
        except SourceUnavailable as exc:
            trend_err = exc.brief()
            if state is not None and state.disable_trends():
                log.warning("本轮禁用趋势：%s", exc.brief())
    out["trend"] = trend_ok

    notes = []
    if serp_err:
        notes.append("SERP 未取到：" + serp_err)
    if trend_err:
        notes.append("趋势未取到：" + trend_err)
    if notes:
        out["notice"] = "；".join(notes)

    # 从缓存或现抓结果里取 SERP 摘要（brief 只读 payload，不再依赖 diag）
    brief = serp_brief(serp_data)
    signal = work_signal(brief)
    try:
        text, model = hotword_llm._llm(build_messages(dict(item, serp=brief),
                                                       prompt_version=prompt_version,
                                                       examples=examples))
    except LLMEmptyContent as exc:
        out["error"] = str(exc)
        out["llm_diag"] = exc.diag
        return _finalize(out, writes)
    except Exception as exc:  # noqa: BLE001
        out["error"] = "LLM 调用失败：" + str(exc)[:160]
        if state is not None:
            n = state.note_llm_failure()
            log.warning("LLM 失败（本轮第 %d 次，继续跑其它词）：%s", n, str(exc)[:120])
        return _finalize(out, writes)
    if not text:
        out["error"] = "LLM 返回空正文（未携带诊断信息，请查 hotwords_llm_empty_content 日志）"
        return _finalize(out, writes)
    out["model"] = model

    verdict, reason = decide(text, signal)
    out["verdict"] = verdict
    hints = signals_of(text)

    _write_result(item, kv, text, hints, verdict, reason, writes, out)
    return _finalize(out, writes)
