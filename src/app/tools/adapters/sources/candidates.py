# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""读取共享 KV 的候选词（只读，不写任何数据）。取代原 radar.py（经 /api/candidates 接口读）。

直读 KV 的 candidates / meta / interests，不调 radar 的 App 接口——radar 把抓取结果写进
同一个 KV（见 google-trend-radar/src/index.js runRefresh），agent 直接读这份共享数据。
Snapshot 原样透出 meta（updatedMs / geosOk / raw / failed）：判断「有没有新热词」看
meta.updatedMs，不能看 candidates 是否非空——累计词库几乎永远非空。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .kv import CloudflareKV, get_kv

log = logging.getLogger(__name__)


def _read(kv: CloudflareKV, status: str, limit: int, light: bool) -> dict[str, Any]:
    candidates = kv.get_json("candidates", []) or []
    meta = kv.get_json("meta", {}) or {}
    interests = kv.get_json("interests", {}) or {} if not light else {}

    items = candidates if not limit else candidates[:limit]
    # 候选条目一般不带 status 字段（status 在 saved/rejected/tags 里），按原 radar.py 语义
    # 默认都算 doing；老条目没有 status 时不能全丢。
    if status:
        filtered = [i for i in items if isinstance(i, dict) and i.get("status", "doing") == status]
        items = filtered or items

    return {"items": items, "meta": meta, "interests": interests}


def snapshot(status: str = "doing", limit: int = 40, light: bool = False,
             kv: Optional[CloudflareKV] = None) -> dict[str, Any]:
    """给 Agent 的完整快照：候选 + meta + interests。status 由调用方按需过滤（radar 不解析该参数）。"""
    kv = kv or get_kv()
    try:
        return _read(kv, status, limit, light)
    except Exception as exc:  # noqa: BLE001
        log.warning("candidates 读取失败：%s", exc)
        return {"items": [], "meta": {}, "error": str(exc), "interests": {}}
