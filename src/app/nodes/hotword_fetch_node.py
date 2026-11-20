# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线取词节点：每轮问一次 tool「下一个未跟进的词」。

    START → fetch_one → analyze_one → route_after_analyze ─┬─ fetch_one（还有词）
                                                          └─ build_summary → END

循环由**图的条件边驱动**（路由在 graph/edges.py），Python 层不写 while/for；
取不到词（None）就置 has_more=False，由条件边收尾。

trace 是声明式切面：取词 / 判例读在函数定义处用 @observed 声明 tool span，
父子靠 contextvar 自动继承，节点里没有任何 parent / run_id 拼装代码。
"""

from __future__ import annotations

import logging
from typing import Any

from src.app.graph.state import HotwordLoopState
from src.app.observability.langsmith import observed
from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from src.app.tools.adapters.hotwords.hotword_kv_io import _kv_list
from src.app.tools.adapters.hotwords.hotword_criteria import pick_examples
from src.app.tools.adapters.hotwords.hotword_contract import select_pending, TOOL_KV_PREFIX

log = logging.getLogger(__name__)


@observed(TOOL_KV_PREFIX + "read-examples", kind="tool",
          inputs=lambda kv: {"key": REJECTED_KEY},
          outputs=lambda rows: {"rejected": len(rows) if isinstance(rows, list) else 0})
def _read_examples(kv: CloudflareKV) -> list:
    return _kv_list(kv, REJECTED_KEY)


@observed(TOOL_KV_PREFIX + "next-candidate", kind="tool",
          inputs=lambda kv, seen: {
              "seenCount": len(seen),
              "keys": ["candidates", SAVED_KEY, REJECTED_KEY, TAGS_KEY, AI_CACHE_PREFIX],
          },
          outputs=lambda res: {"term": (res or {}).get("term", ""),
                               "key": (res or {}).get("key", "")})
def _next_candidate(kv: CloudflareKV, seen: set[str]) -> dict[str, Any] | None:
    return select_pending(kv, seen)


def _load_examples(state: HotwordLoopState) -> dict[str, Any]:
    """首次进入时读一次人工判例；失败只降级掉判例，不让整轮因这张表挂掉。"""
    try:
        examples = pick_examples(_read_examples(state["kv"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("判例读取失败，本轮不带判例继续：%s", str(exc)[:120])
        examples = []
    return {"examples_loaded": True, "examples": examples}


def fetch_one(state: HotwordLoopState) -> dict[str, Any]:
    """问 tool 要下一个词；取到就放进 current，取不到就标记 has_more=False。

    每轮都是一次真实的 tool 调用——没有「首次加载整个候选池、之后游标推进」。
    KV 最终一致可能让刚取的词仍留在候选池里，故传 seen 排除本轮已取过的 key。
    """
    updates: dict[str, Any] = {}
    if not state.get("examples_loaded"):
        updates.update(_load_examples(state))

    seen: set[str] = set(state.get("seen") or set())
    try:
        item = _next_candidate(state["kv"], seen)
    except Exception as exc:  # noqa: BLE001 - KV 不可达 → 整轮失败，不写任何状态
        log.warning("候选查询失败：%s", exc)
        return {**updates, "current": None, "has_more": False,
                "load_error": str(exc)[:200]}

    if item is None:
        return {**updates, "current": None, "has_more": False}

    seen.add(str(item["key"]))
    return {**updates, "current": item, "has_more": True, "seen": seen}
