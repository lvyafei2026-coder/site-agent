# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""结论回写节点：把 AI 正文 / 标签 / 跟进状态写回共享 KV。

「正文先写、正文失败就收手不写状态」这条不变量在 write_hotword_result 工具里，
本节点只负责把它挂到本轮的后置动作链上（名单里没有 write_result 就不连这个节点）。
"""

from __future__ import annotations

from typing import Any

from src.app.graph.state import HotwordLoopState
from src.app.services.tool_service import tool_service
from src.app.tools.adapters.hotwords.hotword_contract import add_error, add_write_errors


def write_result(state: HotwordLoopState) -> dict[str, Any]:
    """回写该词结论；失败只记在这个词身上，不改结论也不影响其它词。"""
    word, ai = state["word"], state.get("ai") or {}
    res = tool_service.run("write_hotword_result", item=state["current"],
                           text=str(ai.get("text") or ""),
                           verdict=str(ai.get("verdict") or ""),
                           reason=str(ai.get("reason") or ""),
                           hints=ai.get("hints") or [])
    if not res["ok"]:
        add_error(word, f"结论回写失败：{res['error']}")
        return {"word": word}

    result = res["result"]
    word["status"] = str(result.get("status") or "")
    add_write_errors(word, result.get("writeErrors") or [])
    return {"word": word}
