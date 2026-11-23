# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""SERP 证据节点：现抓或读缓存拿到搜索结果，摘要喂 AI，作品信号记进硬证据。

本轮走不走这一步由**图的连线**决定（graph/hotword_graph.py 的 evidence 名单），
本节点只管这一步本身：调工具 → 填该词结果 → 源级降级判断。
IO 全在 serp 步骤路由到的那个工具里（tool_service.STEP_TOOLS → tool:fetch_serp span），
本层不碰网络也不打点。
"""

from __future__ import annotations

import logging
from typing import Any

from src.app.enums import HotwordStep
from src.app.graph.state import HotwordLoopState
from src.app.services.tool_service import tool_service
from src.app.tools.adapters.hotwords.criteria import serp_brief, work_signal

log = logging.getLogger(__name__)

STEP = HotwordStep.SERP.value   # 步骤名：名单、降级集合、evidence 键与工具路由都用它


def gather_serp(state: HotwordLoopState) -> dict[str, Any]:
    """补 SERP 辨形证据：源级不可用就把本步关掉，后续词这一步不再发请求。

    evidence["serp"] 由本节点写：True = 拿到结果，False = 试过但没拿到。
    名单里没有这一步时图上根本没有本节点，那个键也就不会出现——前端据此决定列有没有值。
    """
    word, body = state["word"], state["body"]
    signals = list(state.get("signals") or [])
    degrade = state["degrade"]

    if STEP in degrade.disabled:
        word["notice"] = "；".join(x for x in (
            word.get("notice"), "SERP 未取到：本轮已跳过（源不可用）") if x)
        word["evidence"][STEP] = False
        return {"word": word}

    res = tool_service.execute_for_step(STEP, term=word["term"])
    if not res["ok"]:
        word["notice"] = "；".join(x for x in (
            word.get("notice"), f"SERP 未取到：{res['error']}") if x)
        word["evidence"][STEP] = False
        # retryable=True = 源级/网络级不可用：后续词别再撞一遍超时
        if res["retryable"] and degrade.disable(STEP):
            log.warning("SERP 源不可用，后续词跳过这一步（结论缺少辨形线索）：%s", res["error"])
        return {"word": word}

    result = res["result"]
    if result.get("writeErrors"):
        word.setdefault("writeErrors", []).extend(result["writeErrors"])
    word["evidence"][STEP] = bool(result.get("ok"))
    if result.get("error"):
        word["notice"] = "；".join(x for x in (
            word.get("notice"), f"SERP 未取到：{result['error']}") if x)

    brief = serp_brief(result.get("data"))
    body["serp"] = brief                      # 只把前 6 名的域名 + 标题交给 AI
    hard = work_signal(brief)                 # 前 6 名至少 2 条命中才算硬证据
    if hard:
        signals.append(hard)
    return {"word": word, "body": body, "signals": signals}
