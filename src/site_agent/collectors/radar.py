# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""读取 radar 候选词。

radar 私有仓库有 Basic Auth；这里只读它的 /api/candidates，不写任何数据。
若接口路径不同，以 radar 仓库 routes 为准，改这一处即可。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

log = logging.getLogger(__name__)


def candidates(status: str = "doing", limit: int = 20) -> list[dict[str, Any]]:
    base = os.environ.get("RADAR_URL", "").rstrip("/")
    if not base:
        log.warning("未配置 RADAR_URL，跳过候选词读取")
        return []
    try:
        resp = requests.get(
            f"{base}/api/candidates",
            params={"status": status, "limit": limit},
            auth=(os.environ.get("RADAR_USER", ""), os.environ.get("RADAR_PASS", "")),
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning("radar 返回 %s", resp.status_code)
            return []
        data = resp.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        return items[:limit]
    except (requests.RequestException, ValueError) as exc:
        log.warning("radar 读取失败：%s", exc)
        return []
