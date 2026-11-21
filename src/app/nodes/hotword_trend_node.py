# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""趋势证据节点：抓 Google Trends  interest 曲线并落库，**不进提示词**。

与 SERP 节点同构（名单里有 trend 才会被连进子图），差别只在结论用途：
趋势摘要写进 interests 表给词库页与后续判据用，AI 这一轮不看它，
所以这里既不写 body 也不产硬证据。
"""

from __future__ import annotations

import logging
from typing import Any

from src.app.graph.state import HotwordLoopState
from src.app.services.tool_service import tool_service
from src.app.tools.adapters.hotwords.hotword_contract import add_notice, add_write_errors

log = logging.getLogger(__name__)

STEP = "trend"    # 步骤名：名单、降级集合与词结果 evidence 键都用它


def gather_trend(state: HotwordLoopState) -> dict[str, Any]:
    """补趋势证据：源级不可用就把本步关掉，其它步骤照常走。"""
    word = state["word"]
    degrade = state["degrade"]

    if STEP in degrade.disabled:
        add_notice(word, "趋势未取到：本轮已跳过（源不可用）")
        word["evidence"][STEP] = False
        return {"word": word}

    res = tool_service.run("fetch_trend", term=word["term"])
    if not res["ok"]:
        add_notice(word, f"趋势未取到：{res['error']}")
        word["evidence"][STEP] = False
        if res["retryable"] and degrade.disable(STEP):
            log.warning("趋势源不可用，后续词跳过这一步：%s", res["error"])
        return {"word": word}

    result = res["result"]
    add_write_errors(word, result.get("writeErrors") or [])
    word["evidence"][STEP] = bool(result.get("ok"))
    if result.get("error"):
        add_notice(word, f"趋势未取到：{result['error']}")
    return {"word": word}
