# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""热词子系统的**共享契约叶子**：候选筛选 + 判例读取 + 成功阈值 + 词 span 前缀。

依赖方向上的最底层：只依赖 cloudflare.kv、hotword_kv_io 与纯判据 hotword_criteria，
**不 import graph / nodes / services / tools 契约中任何一个**。

谁用它：
    tools/hotwords_tools.py        next_candidate / read_examples 两个工具的实现。
    nodes/hotword_summary_node.py  汇总节点：成功阈值。
    services/hotword_service.py    逐词 span 前缀。
"""

from __future__ import annotations

from typing import Any

from src.app.tools.adapters.cloudflare.kv import (
    get_kv, term_key, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from .hotword_criteria import pick_examples
from .hotword_kv_io import _kv_list, _kv_dict

OK_MIN_RATE = 0.5                    # 整轮成功判定：成功率低于此值即视为失败
WORD_TRACE_PREFIX = "site-agent:hotword:"   # 逐词 span 前缀


def read_examples() -> list[dict[str, str]]:
    """读人工「不做」标记并挑出可喂回 AI 的判例（挑选规则在纯判据层）。"""
    return pick_examples(_kv_list(get_kv(), REJECTED_KEY))


def select_pending(seen: set[str]) -> dict[str, Any] | None:
    """取「下一个」未跟进的热词；没有就返回 None。

    每轮只取一条，由调用方（循环图的 fetch_one 节点经 next_candidate 工具）反复调用
    直到 None 或达上限。
    ★seen 是本轮已取过的 key：KV 最终一致，刚写的状态可能读不到，靠它在**本轮内**
      防止同一个词被重复取出。跨轮的幂等仍由 curated / analyzed 两张 KV 表兜底。

    ★形状兜底：KV 里这几张表是 console/radar 写的，字段形状一旦漂移（比如 tags 变成数组、
    candidates 变成对象），按原样 .items() / 迭代 会整轮失败。这里统一按「期望类型不符就
    当空」处理并 warn，让漂移可见但不阻断分析。
    """
    kv = get_kv()
    candidates = _kv_list(kv, "candidates")
    saved = _kv_list(kv, SAVED_KEY)
    rejected = _kv_list(kv, REJECTED_KEY)
    tags = _kv_dict(kv, TAGS_KEY)

    curated = set()
    for x in (saved + rejected):
        if isinstance(x, dict) and x.get("key"):
            curated.add(term_key(x["key"]))
    for k, v in tags.items():
        if v:
            curated.add(term_key(k))

    analyzed = {k[len(AI_CACHE_PREFIX):] for k in kv.list_keys(AI_CACHE_PREFIX)}

    for c in candidates:
        if not isinstance(c, dict) or not c.get("term"):
            continue
        key = term_key(c.get("key") or c.get("term"))
        if key in curated or key in analyzed or key in seen:
            continue
        return {
            "key": key,
            "term": str(c.get("term") or "")[:120],
            "related": [str(x) for x in (c.get("related") or [])][:6],
            "hints": [str(x) for x in (c.get("hints") or [])][:6],
            "geos": [str(x) for x in (c.get("geos") or [])][:16],
            "geoCount": int(c.get("geoCount") or (len(c.get("geos") or []))),
            "traffic": int(c.get("traffic") or 0),
            "count": int(c.get("count") or 1),
            "score": int(c.get("score") or 0),
            "src": str(c.get("src") or "trends"),
        }
    return None
