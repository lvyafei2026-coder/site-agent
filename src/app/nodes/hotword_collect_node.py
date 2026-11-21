# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""落账节点：一个词的结果进 results、轮次 +1、逐词通道清空。

这是逐词子图的最后一站，**所有分支都要走到这里**（LLM 失败也一样），所以
「失败只落在这个词身上、不中断整轮」不靠 try/except，靠图的连线保证。
收尾规则（写入失败算不算 analyzed）在 hotword_contract.finish_word。
"""

from __future__ import annotations

from typing import Any

from src.app.graph.state import HotwordLoopState
from src.app.tools.adapters.hotwords.hotword_contract import finish_word


def collect_word(state: HotwordLoopState) -> dict[str, Any]:
    """结该词本轮：填好的 word 追加进 results，并把下一步要用的通道归零。"""
    word = finish_word(state["word"])
    return {
        "results": list(state.get("results") or []) + [word],
        "loop_count": state.get("loop_count", 0) + 1,
        "word": {},
        "body": {},
        "signals": [],
        "ai": {},
    }
