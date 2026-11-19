# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""词库整理域：候选池只读快照 + 整理写侧口径（candidates / curation 合并于此）。

读侧（原 candidates.py）：
    直读共享 KV 的 candidates / meta / interests，不调 radar 的 App 接口——radar 把
    抓取结果写进同一个 KV（见 google-trend-radar/src/index.js runRefresh），agent
    直接读这份共享数据。Snapshot 原样透出 meta（updatedMs / geosOk / raw / failed）：
    判断「有没有新热词」看 meta.updatedMs，不能看 candidates 是否非空——累计词库
    几乎永远非空。

写侧（与 console/src/store.js 原样对齐）：
    agent 直写共享 KV，必须和 console 人工操作落在同一套键、同一套状态枚举上——
    否则 agent 写的 saved 和人工点的收藏对不上。这里只动数据结构，不碰网络
    （网络与键名契约在 ../cloudflare/kv.py，全项目唯一的 KV 模块）。

saved 表：[{key, term, status, at, updatedAt, ...}]，status ∈ STATUSES。
rejected 表：[{key, term, reason, source, at, updatedAt}]。
aihints：{ termKey: [信号名] }。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from src.app.enums import CandidateStatus, CurationStatus, HintName, RejectSource

from src.app.tools.adapters.cloudflare.kv import (
    CANDIDATES_KEY, INTERESTS_KEY, CloudflareKV, get_kv, term_key,
)

log = logging.getLogger(__name__)

# 与 console/src/store.js 完全一致；todo = Agent 分析后判定「可以做」自动置入
STATUSES = [s.value for s in CurationStatus]

SAVED_FIELDS = ["term", "related", "hints", "geos", "geoCount", "traffic", "count", "score", "verdict"]
HINT_NAMES = [h.value for h in HintName]


# ---- 读侧：候选池快照 ----

def _read(kv: CloudflareKV, status: str, limit: int, light: bool) -> dict[str, Any]:
    # ★形状兜底：这三张表由 radar / console 写，形状漂移时不能让整个工具调用失败
    candidates = kv.get_json(CANDIDATES_KEY, [])
    if not isinstance(candidates, list):
        log.warning("KV 键 %s 期望数组，实际 %s，按空处理", CANDIDATES_KEY, type(candidates).__name__)
        candidates = []
    meta = kv.get_json("meta", {})
    if not isinstance(meta, dict):
        meta = {}
    interests = {} if light else kv.get_json(INTERESTS_KEY, {})
    if not isinstance(interests, dict):
        interests = {}

    items = candidates if not limit else candidates[:limit]
    # 候选条目一般不带 status 字段（status 在 saved/rejected/tags 里），按原 radar.py 语义
    # 默认都算 doing；老条目没有 status 时不能全丢。
    if status:
        filtered = [i for i in items if isinstance(i, dict)
                    and i.get("status", CandidateStatus.DOING.value) == status]
        items = filtered or items

    return {"items": items, "meta": meta, "interests": interests}


def snapshot(status: str = CandidateStatus.DOING.value, limit: int = 40, light: bool = False,
             kv: Optional[CloudflareKV] = None) -> dict[str, Any]:
    """给 Agent 的完整快照：候选 + meta + interests。status 由调用方按需过滤（radar 不解析该参数）。"""
    kv = kv or get_kv()
    try:
        return _read(kv, status, limit, light)
    except Exception as exc:  # noqa: BLE001
        log.warning("candidates 读取失败：%s", exc)
        return {"items": [], "meta": {}, "error": str(exc), "interests": {}}


# ---- 写侧：saved / rejected / aihints ----

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
        status = (prev.get("status") if prev else CurationStatus.SAVED.value)
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
    source = RejectSource.AI.value if item.get("source") == RejectSource.AI.value else RejectSource.MANUAL.value
    keep_manual = bool(prev and prev.get("source") == RejectSource.MANUAL.value and source == RejectSource.AI.value)
    reason = (str(prev.get("reason") or "")[:300] if keep_manual else (incoming or (prev.get("reason") or "")))
    arr.append({
        "key": key,
        "term": str(item.get("term") or (prev and prev.get("term")) or "").strip()[:120],
        "reason": reason,
        "source": RejectSource.MANUAL.value if keep_manual else source,
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
