# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""读取 radar 候选词（只读，不写任何数据）。

`/api/candidates` 返回 `{meta, candidates, interests}`；`snapshot()` 原样透出
meta（updatedMs / geosOk / raw / failed）。判断「有没有新热词」要看
`meta.updatedMs`，不能看 candidates 是否非空——累计词库几乎永远非空。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

from src.app.config import RADAR_URL

log = logging.getLogger(__name__)


def _fetch(light: bool = False) -> dict[str, Any]:
    base = RADAR_URL.rstrip("/")
    params = {"light": "1"} if light else {}
    resp = requests.get(
        f"{base}/api/candidates",
        params=params,
        auth=(os.environ.get("RADAR_USER", ""), os.environ.get("RADAR_PASS", "")),
        timeout=20,
    )
    if resp.status_code != 200:
        log.warning("radar 返回 %s", resp.status_code)
        return {"items": [], "meta": {}, "error": f"HTTP {resp.status_code}"}
    data = resp.json()
    if not isinstance(data, dict):
        return {"items": [], "meta": {}, "error": "unexpected payload"}
    items = data.get("candidates") or data.get("items") or []
    if isinstance(items, dict):
        items = list(items.values())
    return {"items": items if isinstance(items, list) else [],
            "meta": data.get("meta") or {},
            "interests": data.get("interests") or {}}


def snapshot(status: str = "doing", limit: int = 40,
             light: bool = False) -> dict[str, Any]:
    """给 Agent 的完整快照：候选 + meta。status 由调用方按需过滤（radar 不解析该参数）。"""
    try:
        data = _fetch(light=light)
    except (requests.RequestException, ValueError) as exc:
        log.warning("radar 读取失败：%s", exc)
        return {"items": [], "meta": {}, "error": str(exc)}

    items = data["items"]
    if status:
        filtered = [i for i in items
                    if not isinstance(i, dict) or i.get("status", "doing") == status]
        # 老候选条目没有 status 字段时不能全丢
        items = filtered or items
    return {"items": items[:limit], "meta": data["meta"],
            "interests": data.get("interests", {})}
