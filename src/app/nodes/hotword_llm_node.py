# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""AI 判定节点：素材拼提示词 → 调 LLM → 结论解析，硬证据与红线把结论压回「不做」。

LLM 不是工具（横切能力，与常规线一样不经 registry）：传输在 src/app/utils/llm_io，
提示词拼装与 max_tokens 口径在 src/app/prompts/hotword_messages；从 AI 正文里读结论的
四个解析函数（verdict_of / redline_of / signals_of / decide）只有本节点用，就写在这里。

★失败不抛给上层，只写进该词 error：LLM 挂掉的这个词不进步后置动作
  （route_after_llm 直接送它去落账），其它词照常跑完。
★解析格式对应 D1 提示词的 8 小节（小节号会变，所以只认「结论」「信号标签」字样），
  改措辞要同步 test/test_prompts.py 的结构契约。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.app.enums import HintName, Verdict
from src.app.graph.state import HotwordLoopState
from src.app.prompts.hotword_messages import MAX_TOKENS, build_messages
from src.app.utils import llm_io

log = logging.getLogger(__name__)

HINT_NAMES = tuple(h.value for h in HintName)


def verdict_of(text: str) -> str:
    """最后一节「结论」：做 / 观望 / 不做；小节号可能变，不写死数字。"""
    s = str(text or "")
    m = re.search(r"\d+\)\s*结论[：:]\s*(不做|观望|做)", s)
    if m:
        return m.group(1)
    if re.search(r"结论[：:][^\n]*不做", s):
        return Verdict.SKIP.value
    return ""


def redline_of(text: str) -> str:
    """AI 漏写「不做」时的兜底：真人姓名 / 影视作品，宁可错杀。"""
    s = str(text or "")
    if re.search(r"(真人姓名|姓名权|肖像权|侵犯.{0,4}权|侵权风险)", s):
        return "真人姓名，涉姓名权/肖像权"
    if (re.search(r"(影视作品|电影|剧集|作品名|专辑|小说|番剧)", s)
            and re.search(r"(版权|侵权|合规|盗版)", s)):
        return "影视/作品名，涉版权与合规风险"
    return ""


def signals_of(text: str) -> list[str] | None:
    """第 8 节信号标签；解析不出返回 None（保持原样），写「无」返回空列表。"""
    m = re.search(r"(?:^|\n)\s*8\)\s*信号标签[：:]\s*([^\n]+)", str(text or ""))
    if not m:
        return None
    line = m.group(1).strip()
    if line.startswith("无"):
        return []
    out = [h for h in HINT_NAMES if h in line][:3]
    return out or None


def decide(text: str, signal: str = "") -> tuple[str, str]:
    """结论 + 理由。红线与 SERP 作品硬证据都能把结论压成「不做」。"""
    verdict = verdict_of(text)
    reason = ""
    risk = redline_of(text)
    if verdict != Verdict.SKIP.value and risk:
        verdict, reason = Verdict.SKIP.value, risk
    if verdict != Verdict.SKIP.value and signal:
        verdict, reason = Verdict.SKIP.value, signal
    return verdict, reason


def llm_analyze(state: HotwordLoopState) -> dict[str, Any]:
    """读 body / signals 出结论，正文与解析结果放进 ai 通道给后置动作节点。"""
    word = state["word"]
    degrade = state["degrade"]
    try:
        text, model = llm_io.complete(build_messages(
            state["body"], prompt_version=state.get("prompt_version"),
            examples=state.get("examples") or []), max_tokens=MAX_TOKENS)
    except llm_io.LLMEmptyContent as exc:
        word["error"] = "；".join(x for x in (word["error"], str(exc)) if x)
        word["llm_diag"] = exc.diag
        return {"word": word}
    except Exception as exc:  # noqa: BLE001
        msg = "LLM 调用失败：" + str(exc)[:160]
        word["error"] = "；".join(x for x in (word["error"], msg) if x)
        log.warning("LLM 失败（本轮第 %d 次，继续跑其它词）：%s",
                    degrade.note_llm_failure(), str(exc)[:120])
        return {"word": word}
    if not text:
        msg = "LLM 返回空正文（未携带诊断信息，请查 llm_empty_content 日志）"
        word["error"] = "；".join(x for x in (word["error"], msg) if x)
        return {"word": word}

    verdict, reason = decide(text, "；".join(state.get("signals") or []))
    word["model"] = model
    word["verdict"] = verdict
    return {"word": word,
            "ai": {"text": text, "verdict": verdict, "reason": reason,
                   "hints": signals_of(text)}}
