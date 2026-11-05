# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""Reddit 只读搜索（公共 JSON 接口，不登录、不发帖）。

只用于"发现外链机会"，永不用于自动发帖（设计红线 §1.4）。
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

UA = "site-agent/0.1 (read-only research; contact: hello@toolara.dev)"


def search(query: str, limit: int = 15, timeframe: str = "week") -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": query, "sort": "new", "t": timeframe, "limit": limit},
            headers={"User-Agent": UA},
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning("reddit 返回 %s（可能被限流）", resp.status_code)
            return []
        children = resp.json().get("data", {}).get("children", [])
        out = []
        for item in children:
            d = item.get("data", {})
            out.append(
                {
                    "title": d.get("title", ""),
                    "subreddit": d.get("subreddit", ""),
                    "url": f"https://www.reddit.com{d.get('permalink', '')}",
                    "score": d.get("score", 0),
                    "num_comments": d.get("num_comments", 0),
                    "created_utc": d.get("created_utc", 0),
                }
            )
        return out
    except (requests.RequestException, ValueError) as exc:
        log.warning("reddit 查询失败：%s", exc)
        return []
