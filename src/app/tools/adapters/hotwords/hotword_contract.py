# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""热词子系统的**共享契约叶子**：候选筛选规则 + trace 常量 + 成功阈值。

依赖方向上的最底层：只依赖 cloudflare.kv 与 hotword_kv_io（都是更底层的叶子），
**不 import graph / nodes / services 中任何一个**——这正是它能打破上下层环的原因。

谁用它：
    nodes/hotword_fetch_node.py    取词节点：每轮问 tool 取一词、KV 工具 span 命名。
    nodes/hotword_summary_node.py  汇总节点：成功阈值。
    hotwords.py              adapters 门面：re-export 本层符号（含 RUN_TRACE_NAME 别名）。
"""

from __future__ import annotations

from typing import Any

from src.app.enums import TraceName
from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, term_key, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from .hotword_kv_io import _kv_list, _kv_dict

OK_MIN_RATE = 0.5                    # 整轮成功判定：成功率低于此值即视为失败
RUN_TRACE_NAME = TraceName.HOTWORD_RUN      # 一轮的根 span（真源 enums.TraceName）
WORD_TRACE_PREFIX = "site-agent:hotword:"   # 逐词 span 前缀
TOOL_KV_PREFIX = "tool:kv."          # 取词 / 判例读等 KV 工具 span 前缀


def select_pending(kv: CloudflareKV, seen: set[str]) -> dict[str, Any] | None:
    """问 tool 要「下一个」未跟进的热词；没有就返回 None。

    每轮只取一条，由调用方（循环图的 fetch_one 节点）反复调用直到 None 或达上限。
    ★seen 是本轮已取过的 key：KV 最终一致，刚写的状态可能读不到，靠它在**本轮内**
      防止同一个词被重复取出。跨轮的幂等仍由 curated / analyzed 两张 KV 表兜底。

    ★形状兜底：KV 里这几张表是 console/radar 写的，字段形状一旦漂移（比如 tags 变成数组、
    candidates 变成对象），按原样 .items() / 迭代 会整轮失败。这里统一按「期望类型不符就
    当空」处理并 warn，让漂移可见但不阻断分析。
    """
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
