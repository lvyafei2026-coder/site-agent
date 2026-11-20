# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线分析节点：把 current 一词走完「补数据 → LLM → 回写状态」。

单词的业务逻辑在 services/hotword_service.py（已被 @observed 装饰成词 span），
本节点只负责推进循环计数与结果累积；失败只落在该词自己身上，不中断整轮。
"""

from __future__ import annotations

from typing import Any

from src.app.graph.state import HotwordLoopState
from src.app.services.hotword_service import _analyze_one


def analyze_one(state: HotwordLoopState) -> dict[str, Any]:
    """分析 current 一词，结果 append；无词（tool 已取尽）时是 no-op。"""
    item = state.get("current")
    results = list(state.get("results", []))
    loop_count = state.get("loop_count", 0)

    # 条件边保证只在有词时走到这里；取尽时 current=None，直接放过。
    if not item:
        return {"results": results, "loop_count": loop_count}

    result = _analyze_one(
        item, state.get("degrade"),
        state.get("prompt_version"), state.get("examples", []),
    )
    results.append(result)
    return {"results": results, "loop_count": loop_count + 1}
