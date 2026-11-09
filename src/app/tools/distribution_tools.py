# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""distribution 域：外链台账、Reddit 只读搜索、Dev.to 读写（定义 + 实现同文件）。"""

from __future__ import annotations

from typing import Any

import requests
from pydantic import BaseModel, Field

from src.app.tools.adapters.cloudflare import database
from src.app.tools.adapters.cloudflare.sql import Sql
from src.app.tools.adapters.publishing import devto
from src.app.tools.adapters.sources import reddit
from src.app.tools.base import RuntimeTool


class DistributionOps:
    """distribution 域的实现：D1 台账 + Reddit / Dev.to。"""

    def backlink_ledger(self, limit=200):
        return database.query(Sql.BACKLINK_LEDGER, [limit])

    def reddit_search(self, query, limit=8):
        return reddit.search(query, limit=limit)

    def devto_articles(self, tags=None):
        out = []
        for tag in (tags or ["ai", "webdev", "productivity"]):
            try:
                resp = requests.get(
                    "https://dev.to/api/articles",
                    params={"tag": tag, "top": 7, "per_page": 5}, timeout=20,
                )
                if resp.status_code != 200:
                    continue
                for art in resp.json():
                    out.append({
                        "channel": "devto", "title": art.get("title", ""),
                        "url": art.get("url", ""), "tags": art.get("tag_list", []),
                        "reactions": art.get("public_reactions_count", 0),
                    })
            except (requests.RequestException, ValueError):
                continue
        return out

    def publish_devto(self, payload):
        return devto.publish(payload)


class GetBacklinkLedgerTool(RuntimeTool):
    """读取外链台账中未失效的记录，用于机会去重。"""

    tool_name = "get_backlink_ledger"
    method = "backlink_ledger"

    class Input(BaseModel):
        limit: int = Field(200, description="最多返回几条")


class SearchRedditTool(RuntimeTool):
    """只读搜索 Reddit 讨论，用于发现外链机会。永不用于发帖。"""

    tool_name = "search_reddit"
    method = "reddit_search"

    class Input(BaseModel):
        query: str = Field(..., description="搜索词")
        limit: int = Field(8, description="最多返回几条")


class ListDevtoArticlesTool(RuntimeTool):
    """按标签列出 Dev.to 热门文章，用于寻找可投稿的技术话题。"""

    tool_name = "list_devto_articles"
    method = "devto_articles"

    class Input(BaseModel):
        tags: list = Field(['ai', 'webdev', 'productivity'], description="标签列表")


class PublishDevtoArticleTool(RuntimeTool):
    """通过官方 API 在 Dev.to 发布一篇技术文章。不可逆：只能由请求显式指定 tools 触发。"""

    tool_name = "publish_devto_article"
    method = "publish_devto"
    kind = "write"

    class Input(BaseModel):
        payload: dict[str, Any] = Field(..., description="文章字段：title/draft/target_url/...")
