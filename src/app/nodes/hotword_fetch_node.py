# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线取词节点：每轮经 next_candidate 工具要一个词，并把逐词通道铺好。

    START → fetch_one → route_after_fetch ─┬─ analyze_word（逐词子图）→ 回环 / 收尾
                                          └─ build_summary → END（取尽 / 名单非法）

循环由**图的条件边驱动**（路由在 graph/edges.py），Python 层不写 while/for；
取不到词（result 为 None）就置 has_more=False，由条件边收尾。

IO 一律走工具，与常规线同一套契约：本节点只说步骤名（examples 读判例、fetch 取词），
步骤 → 工具的路由表在 services/tool_service.py。tool span 由 registry 的统一 tracer
建，节点不自打点。
"""

from __future__ import annotations

import logging
from typing import Any

from src.app.enums import HotwordStep
from src.app.graph.state import HotwordLoopState
from src.app.services.tool_service import tool_service
from src.app.tools.adapters.hotwords.hotword_contract import new_word

log = logging.getLogger(__name__)


def _load_examples() -> dict[str, Any]:
    """首次进入时读一次人工判例；失败只降级掉判例，不让整轮因这张表挂掉。"""
    res = tool_service.execute_for_step(HotwordStep.EXAMPLES.value)
    if not res["ok"]:
        log.warning("判例读取失败，本轮不带判例继续：%s", res["error"][:120])
        return {"examples_loaded": True, "examples": []}
    return {"examples_loaded": True, "examples": res["result"]}


def fetch_one(state: HotwordLoopState) -> dict[str, Any]:
    """问工具要下一个词；取到就放进 current，取不到就标记 has_more=False。

    每轮都是一次真实的工具调用——没有「首次加载整个候选池、之后游标推进」。
    KV 最终一致可能让刚取的词仍留在候选池里，故传 seen 排除本轮已取过的 key。
    load_error 非空 = 入口已判定本轮跑不起来（步骤名单非法），不再碰 KV，直接交给收尾。
    """
    if state.get("load_error"):
        return {}

    updates: dict[str, Any] = {}
    if not state.get("examples_loaded"):
        updates.update(_load_examples())

    seen: set[str] = set(state.get("seen") or set())
    res = tool_service.execute_for_step(HotwordStep.FETCH.value, seen=sorted(seen))
    if not res["ok"]:
        # KV 不可达 → 整轮失败，不写任何状态
        log.warning("候选查询失败：%s", res["error"])
        return {**updates, "current": None, "has_more": False,
                "load_error": "候选查询失败：" + res["error"][:200]}

    item = res["result"]
    if item is None:
        return {**updates, "current": None, "has_more": False}

    seen.add(str(item["key"]))
    # 取到词就同时把逐词通道铺好：word = 该词结果骨架，body = 喂 AI 的素材起点，
    # 之后每个补证据节点往这两个通道里填，collect_word 落账时清空。
    return {**updates, "current": item, "has_more": True, "seen": seen,
            "word": new_word(item["term"]), "body": dict(item),
            "signals": [], "ai": {}}