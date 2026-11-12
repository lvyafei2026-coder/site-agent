# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""整理侧写侧口径：从 console/src/store.js 原样移植。

agent 现在直写共享 KV，必须和 console 人工操作落在同一套键、同一套状态枚举上——
否则 agent 写的 saved 和人工点的收藏对不上。这里只动数据结构，不碰网络（网络在 kv.py）。

saved 表：[{key, term, status, at, updatedAt, ...}]，status ∈ STATUSES。
rejected 表：[{key, term, reason, source, at, updatedAt}]。
aihints：{ termKey: [信号名] }。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .kv import term_key

# 与 console/src/store.js 完全一致；todo = Agent 分析后判定「可以做」自动置入
STATUSES = ["saved", "todo", "wip", "live", "parked"]

SAVED_FIELDS = ["term", "related", "hints", "geos", "geoCount", "traffic", "count", "score", "verdict"]
HINT_NAMES = ["工具查询", "价格规格", "日程日期", "资格政策", "用法教程", "对比评测"]


def _pick_saved(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in SAVED_FIELDS:
        v = item.get(f)
        if v is not None:
            out[f] = v
    if isinstance(out.get("related"), list):
        out["related"] = [str(x) for x in out["related"][:6]]
    if isinstance(out.get("hints"), list):
        out["hints"] = [str(x) for x in out["hints"][:6]]
    if isinstance(out.get("geos"), list):
        out["geos"] = [str(x) for x in out["geos"][:16]]
    if isinstance(out.get("term"), str):
        out["term"] = out["term"][:120]
    if isinstance(out.get("verdict"), str):
        out["verdict"] = out["verdict"][:12]
    return out


def upsert_saved(list_: Optional[list], item: dict[str, Any], now: Optional[int] = None) -> list:
    """新增或更新一条收藏；同词去重，首次收藏时间不变，更新 updatedAt。

    status 只认 STATUSES 里的；传错或缺失时保留原状态，首次默认 saved。
    """
    now = now or int(time.time() * 1000)
    key = term_key(item.get("term"))
    if not key:
        return list_ if isinstance(list_, list) else []
    arr = [x for x in (list_ or []) if isinstance(x, dict) and x.get("key") != key]
    prev = next((x for x in (list_ or []) if isinstance(x, dict) and x.get("key") == key), None)
    status = item.get("status")
    if status not in STATUSES:
        status = (prev.get("status") if prev else "saved")
    merged = {**_pick_saved(prev or {}), **_pick_saved(item), "key": key, "status": status}
    merged["at"] = (prev.get("at") if prev else now)
    merged["updatedAt"] = now
    arr.append(merged)
    arr.sort(key=lambda x: x.get("at", 0), reverse=True)
    return arr


def upsert_rejected(list_: Optional[list], item: dict[str, Any], now: Optional[int] = None) -> list:
    """标记「不做」；人工标记优先：AI 重复判定不改原因、不降级来源。"""
    now = now or int(time.time() * 1000)
    key = term_key(item.get("term"))
    if not key:
        return list_ if isinstance(list_, list) else []
    arr = [x for x in (list_ or []) if isinstance(x, dict) and x.get("key") != key]
    prev = next((x for x in (list_ or []) if isinstance(x, dict) and x.get("key") == key), None)
    incoming = str(item.get("reason") or "").strip()[:300]
    source = "ai" if item.get("source") == "ai" else "manual"
    keep_manual = bool(prev and prev.get("source") == "manual" and source == "ai")
    reason = (str(prev.get("reason") or "")[:300] if keep_manual else (incoming or (prev.get("reason") or "")))
    arr.append({
        "key": key,
        "term": str(item.get("term") or (prev and prev.get("term")) or "").strip()[:120],
        "reason": reason,
        "source": "manual" if keep_manual else source,
        "at": (prev.get("at") if prev else now),
        "updatedAt": now,
    })
    arr.sort(key=lambda x: x.get("at", 0), reverse=True)
    return arr


def set_hint_override(map_: Optional[dict], term: str, hints: list) -> dict:
    """覆盖某词的信号标签；只保留 HINT_NAMES 内的、最多 3 个。"""
    out = dict(map_ or {})
    key = term_key(term)
    if not key:
        return out
    out[key] = [
        str(h) for h in (hints or [])
        if str(h) in HINT_NAMES
    ][:3]
    return out
