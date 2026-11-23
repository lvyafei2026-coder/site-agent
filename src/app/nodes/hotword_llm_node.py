# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""AI 判定节点：素材拼提示词 → 调 LLM → 结论解析，硬证据与红线把结论压回「不做」。

LLM 不是工具（横切能力，与常规线一样不经 registry）：传输在 src/app/utils/llm_io，
提示词拼装与 max_tokens 口径在 src/app/prompts/hotword_messages，判定与解析口径在
services/hotword_criteria，本节点只做调用与收口。

★失败不抛给上层，只写进该词 error：LLM 挂掉的这个词不进步后置动作
  （route_after_llm 直接送它去落账），其它词照常跑完。
"""

from __future__ import annotations

import logging
from typing import Any

from src.app.graph.state import HotwordLoopState
from src.app.prompts.hotword_messages import MAX_TOKENS, build_messages
from src.app.services.hotword_criteria import decide, signals_of
from src.app.utils import llm_io

log = logging.getLogger(__name__)


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
