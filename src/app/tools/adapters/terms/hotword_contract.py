# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""热词子系统的**共享契约叶子**：候选筛选规则 + trace 常量 + 成功阈值。

依赖方向上的最底层：只依赖 cloudflare.kv 与 hotword_kv_io（都是更底层的叶子），
**不 import batch / loop / builder 中任何一个**——这正是它能打破 batch↔loop 环的原因。

谁用它：
    hotword_batch.py  批次外壳：筛候选、批次 trace 命名、汇总成功阈值。
    hotword_loop.py   循环节点：筛候选（首次懒加载）、词 span 命名、汇总。
    hotwords.py       门面：re-export 给测试与外部按 hotwords.<name> 引用。
"""

from __future__ import annotations

from typing import Any

from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, term_key, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from .hotword_kv_io import _kv_list, _kv_dict

BATCH_OK_MIN_RATE = 0.5     # 整批成功判定：成功率低于此值即视为失败
BATCH_TRACE_NAME = "site-agent:hotword-batch"
WORD_TRACE_PREFIX = "site-agent:hotword:"


def _select_pending(kv: CloudflareKV, limit: int) -> list[dict[str, Any]]:
    """读 KV 候选池，剔除已跟进/打标签/已分析的词，返回前 limit 条未跟进候选。

    ★形状兜底：KV 里这几张表是 console/radar 写的，字段形状一旦漂移（比如 tags 变成数组、
    candidates 变成对象），按原样 .items() / 迭代 会整批失败。这里统一按「期望类型不符就
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

    items: list[dict[str, Any]] = []
    for c in candidates:
        if not isinstance(c, dict) or not c.get("term"):
            continue
        key = term_key(c.get("key") or c.get("term"))
        if key in curated or key in analyzed:
            continue
        items.append({
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
        })
        if len(items) >= limit:
            break
    return items
