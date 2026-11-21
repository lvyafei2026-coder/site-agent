# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的单词编排层：一个词走完「补证据 → LLM → 后置动作」三段通用流程。

本层**不认识具体工具名**：每一步调哪个工具、怎么绑参、结果怎么喂提示词 / 怎么回填，
全声明在 services/hotword_plan.py 的 Evidence / PostAction 里，这里只按计划遍历执行。
加一个证据源或后置动作 = plan 里加一条声明，本文件与循环、节点、图、请求模型都不用改。

与常规线同构：IO 一律经 tool_service 调 registry 里的工具，统一拿 ToolOutput、
统一 retryable 分类、统一 tool span。LLM 与纯判据不是工具（横切能力）：
传输在 hotword_llm、判据在 hotword_criteria。

★不抛异常给上层：证据/LLM/KV 任一环出问题都写进 items[].error，其它词继续跑。
★源级降级是**编排级**决策：工具报 ok=False + retryable=True（SourceUnavailable / 网络层）
  就把该步骤 key 记进 degrade.disabled，后续词跳过这一步——跳过的是「取数」，不是整词分析。
  跳过时没有工具调用，也就不伪造 tool span，只用 note() 在词 span 上留一条痕。
★写入明细由各工具自报（result.writes / writeErrors），本层跨步骤汇总进该词 error。
★trace 是声明式切面：_analyze_one 在定义处用 @observed 声明词 span，父子靠 contextvar
  自动继承——函数体不拼 parent / run_id。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.app.observability.langsmith import note, observed
from src.app.services.hotword_plan import (
    DEFAULT_EVIDENCE_STEPS, DEFAULT_POST_STEPS, Evidence, PostAction,
)
from src.app.services.tool_service import tool_service
from src.app.tools.adapters.hotwords.hotword_contract import WORD_TRACE_PREFIX
from src.app.tools.adapters.hotwords.hotword_criteria import build_messages, decide, signals_of
from src.app.tools.adapters.hotwords import hotword_llm
from src.app.tools.adapters.hotwords.hotword_llm import LLMEmptyContent

log = logging.getLogger(__name__)


class _DegradeState:
    """整轮**降级状态**：哪些证据步骤本轮已关掉，外加 LLM 失败次数。

    单词级失败隔离后这里只保留「省时间 / 供汇总」两件事，不拦任何词：

      ・某步源不可用 → disabled.add(key)：后续词跳过这一步（省掉每词几发超时），照常走 LLM；
      ・LLM 失败      → 只累加次数供汇总，不中止。

    源级故障是「出口 IP 不通」这类全局问题，让 20 个词各撞一遍超时毫无意义；
    跳过的是「取数」这一步，不是整词分析。

    热词图逐词串行（无常驻线程），状态只在本轮内单向推进，无需加锁。
    """

    def __init__(self) -> None:
        self.disabled: set[str] = set()
        self.llm_failures = 0

    def disable(self, key: str) -> bool:
        """关掉一个证据步骤；返回是否首次置位（用于只打一条日志）。"""
        first = key not in self.disabled
        self.disabled.add(key)
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
    """逐词 span 的 outputs：结论、状态、补证情况与写入失败明细。"""
    keys = ("verdict", "status", "model", "evidence", "notice",
            "writeErrors", "error", "llm_diag")
    return {k: out[k] for k in keys if k in out}


def _word_error(out: dict[str, Any]) -> str | None:
    """业务失败也让 span 标红：_analyze_one 不抛异常，trace 自己看不到失败。"""
    return out.get("error") or None


@observed(_word_span_name, kind="chain", outputs=_word_outputs, error=_word_error)
def _analyze_one(item: dict[str, Any],
                 state: Optional[_DegradeState] = None,
                 prompt_version: Optional[str] = None,
                 examples: Optional[list[dict[str, str]]] = None,
                 evidence: tuple[Evidence, ...] = DEFAULT_EVIDENCE_STEPS,
                 post: tuple[PostAction, ...] = DEFAULT_POST_STEPS) -> dict[str, Any]:
    """一个词按计划走完三段，失败只落在这个词自己身上。

    evidence / post 是已解析的步骤元组（hotword_plan.resolve_plan）；不传就是默认计划。
    """
    term = str(item.get("term") or "").strip()
    out: dict[str, Any] = {"term": term, "verdict": "", "status": "", "model": "",
                           "evidence": {s.key: False for s in evidence}, "error": ""}
    if not term:
        out["error"] = "空词"
        return out

    write_errors: list[str] = []
    notices: list[str] = []
    signals: list[str] = []
    body = dict(item)
    ctx: dict[str, Any] = {"item": item, "term": term}

    # ---- ① 补证据：读缓存 / 现抓 / 写回全在工具里（tool:<name> span）----
    for step in evidence:
        if state is not None and step.key in state.disabled:
            notices.append(f"{step.label} 未取到：本轮已跳过（源不可用）")
            note(**{step.key + "Skipped": True})
            continue
        res = tool_service.run(step.tool, **step.bind(ctx))
        if not res["ok"]:
            notices.append(f"{step.label} 未取到：{res['error']}")
            # retryable=True = 源级/网络级不可用：后续词别再撞一遍超时
            if res["retryable"] and state is not None and state.disable(step.key):
                log.warning("%s 源不可用，后续词跳过这一步：%s", step.label, res["error"])
            continue
        result = res["result"]
        write_errors.extend(result.get("writeErrors") or [])
        out["evidence"][step.key] = bool(result.get("ok"))
        if result.get("error"):
            notices.append(f"{step.label} 未取到：{result['error']}")
        if not result.get("ok") or not step.feed:
            continue
        shaped = step.shape(result.get("data"))
        body[step.key] = shaped
        if step.signal:
            hard = step.signal(shaped)
            if hard:
                signals.append(hard)
    if notices:
        out["notice"] = "；".join(notices)

    # ---- ② LLM：body 带着各步喂进来的证据，硬证据能把结论压成「不做」----
    try:
        text, model = hotword_llm._llm(build_messages(body, prompt_version=prompt_version,
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

    verdict, reason = decide(text, "；".join(signals))
    out["verdict"] = verdict
    ctx.update(text=text, verdict=verdict, reason=reason, hints=signals_of(text))

    # ---- ③ 后置动作：正文先写、失败即收手的不变量在工具里 ----
    for step in post:
        res = tool_service.run(step.tool, **step.bind(ctx))
        if res["ok"]:
            step.apply(out, res["result"])
            write_errors.extend(res["result"].get("writeErrors") or [])
        else:
            detail = f"{step.label}失败：{res['error']}"
            out["error"] = (out["error"] + "；" if out["error"] else "") + detail
    return _finalize(out, write_errors)