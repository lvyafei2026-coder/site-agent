# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的单词编排层：一个词走完「补数据 → LLM → 结论解析 → 回写状态」。

与常规线同构：IO 一律经 tool_service 调 registry 里的工具（fetch_serp / fetch_trend /
write_hotword_result），统一拿 ToolOutput、统一 retryable 分类、统一 tool span。
本层只做编排与判读——纯判据在 hotword_criteria、LLM 传输在 hotword_llm，两者都不是
工具（常规线的 LLM 同样走 llm/completion 而不经 registry）。

★不抛异常给上层：SERP/Trends/LLM/KV 任一环出问题都写进 items[].error，其它词继续跑。
★源级降级是**编排级**决策：工具报 ok=False + retryable=True（SourceUnavailable / 网络层）
  就置位 serp_off / trends_off，后续词跳过该源——跳过的是「取数」这一步，不是整词分析。
  跳过时没有工具调用，也就不伪造 tool span，只用 note() 在词 span 上留一条痕。
★写入明细由各工具自报（result.writes / writeErrors），本层跨工具汇总进该词 error。
★trace 是声明式切面：_analyze_one 在定义处用 @observed 声明词 span，父子靠 contextvar
  自动继承——函数体不拼 parent / run_id。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.app.observability.langsmith import note, observed
from src.app.services.tool_service import tool_service
from src.app.tools.adapters.hotwords.hotword_contract import WORD_TRACE_PREFIX
from src.app.tools.adapters.hotwords.hotword_criteria import (
    serp_brief, work_signal, build_messages, decide, signals_of,
)
from src.app.tools.adapters.hotwords import hotword_llm
from src.app.tools.adapters.hotwords.hotword_llm import LLMEmptyContent

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


def _finalize(out: dict[str, Any], write_errors: list[str]) -> dict[str, Any]:
    """把本词所有工具的写入失败汇总进 error —— 有任何一个写失败就不算 analyzed。"""
    if write_errors:
        out["writeErrors"] = list(write_errors)
        detail = "KV 写入失败：" + "、".join(write_errors)
        out["error"] = (out["error"] + "；" if out["error"] else "") + detail
    return out


def _word_span_name(item: dict, *args, **kwargs) -> str:
    return WORD_TRACE_PREFIX + str(item.get("term") or item.get("key") or "")[:60]


def _word_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """逐词 span 的 outputs：结论、状态、补数情况与写入失败明细。"""
    keys = ("verdict", "status", "model", "serp", "trend", "notice",
            "writeErrors", "error", "llm_diag")
    return {k: out[k] for k in keys if k in out}


def _word_error(out: dict[str, Any]) -> str | None:
    """业务失败也让 span 标红：_analyze_one 不抛异常，trace 自己看不到失败。"""
    return out.get("error") or None


@observed(_word_span_name, kind="chain", outputs=_word_outputs, error=_word_error)
def _analyze_one(item: dict[str, Any],
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

    write_errors: list[str] = []

    # ---- SERP：读缓存 / 现抓 / 写回，全在 fetch_serp 工具里（tool:fetch_serp span）----
    serp_data: dict | None = None
    serp_err = ""
    if state is not None and state.serp_off:
        serp_err = "本轮已跳过 SERP（源不可用）"
        note(serpSkipped=True)
    else:
        res = tool_service.run("fetch_serp", term=term)
        if res["ok"]:
            serp_data = res["result"]["data"]
            serp_err = res["result"]["error"]
            write_errors.extend(res["result"]["writeErrors"])
        else:
            serp_err = res["error"]
            # retryable=True = 源级/网络级不可用：后续词别再撞一遍超时
            if res["retryable"] and state is not None and state.disable_serp():
                log.warning("SERP 源不可用，后续词跳过 SERP（结论缺少辨形线索）：%s", serp_err)
    out["serp"] = serp_data is not None

    # ---- Trends：同上，工具是 fetch_trend ----
    trend_ok = False
    trend_err = ""
    if state is not None and state.trends_off:
        trend_err = "本轮已禁用趋势（源不可用）"
        note(trendSkipped=True)
    else:
        res = tool_service.run("fetch_trend", term=term)
        if res["ok"]:
            trend_ok = bool(res["result"]["ok"])
            trend_err = res["result"]["error"]
            write_errors.extend(res["result"]["writeErrors"])
        else:
            trend_err = res["error"]
            if res["retryable"] and state is not None and state.disable_trends():
                log.warning("本轮禁用趋势：%s", trend_err)
    out["trend"] = trend_ok

    notes = []
    if serp_err:
        notes.append("SERP 未取到：" + serp_err)
    if trend_err:
        notes.append("趋势未取到：" + trend_err)
    if notes:
        out["notice"] = "；".join(notes)

    # 从缓存或现抓结果里取 SERP 摘要（brief 只读 payload，纯判据无 IO）
    brief = serp_brief(serp_data)
    signal = work_signal(brief)
    try:
        text, model = hotword_llm._llm(build_messages(dict(item, serp=brief),
                                                       prompt_version=prompt_version,
                                                       examples=examples))
    except LLMEmptyContent as exc:
        out["error"] = str(exc)
        out["llm_diag"] = exc.diag
        return _finalize(out, write_errors)
    except Exception as exc:  # noqa: BLE001
        out["error"] = "LLM 调用失败：" + str(exc)[:160]
        if state is not None:
            n = state.note_llm_failure()
            log.warning("LLM 失败（本轮第 %d 次，继续跑其它词）：%s", n, str(exc)[:120])
        return _finalize(out, write_errors)
    if not text:
        out["error"] = "LLM 返回空正文（未携带诊断信息，请查 hotwords_llm_empty_content 日志）"
        return _finalize(out, write_errors)
    out["model"] = model

    verdict, reason = decide(text, signal)
    out["verdict"] = verdict
    hints = signals_of(text)

    # ---- 回写：正文先写、失败即收手的不变量在 write_hotword_result 工具里 ----
    res = tool_service.run("write_hotword_result", item=item, text=text,
                           verdict=verdict, reason=reason, hints=hints)
    if res["ok"]:
        out["status"] = res["result"]["status"]
        write_errors.extend(res["result"]["writeErrors"])
    else:
        detail = "结论回写失败：" + res["error"]
        out["error"] = (out["error"] + "；" if out["error"] else "") + detail
    return _finalize(out, write_errors)