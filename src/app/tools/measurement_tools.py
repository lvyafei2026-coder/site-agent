# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""measurement 域：埋点聚合与 Search Console 取数（定义 + 实现同文件）。"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from src.app.tools.adapters.cloudflare import database
from src.app.tools.adapters.cloudflare.sql import Sql
from src.app.tools.adapters.sources import gsc
from src.app.tools.base import RuntimeTool


class MeasurementOps:
    """measurement 域的实现：只读 D1 埋点与 GSC。"""

    def click_stats(self, days=7):
        since = int(time.time() * 1000) - days * 86400_000
        out = {}
        for key, sql in (("daily", Sql.DAILY_STATS), ("top_pages", Sql.TOP_PAGES)):
            try:
                out[key] = database.query(sql, [since])
            except Exception:  # noqa: BLE001 - 采集失败不阻塞决策
                out[key] = []
        return out

    def search_analytics(self, days=7, dimensions=("page",)):
        return gsc.search_analytics(days=days, dimensions=tuple(dimensions))


class GetClickStatsTool(RuntimeTool):
    """按站点/天聚合最近 N 天埋点，返回 daily（PV、UV）与 top_pages。UV 用 COUNT(DISTINCT sid)。"""

    tool_name = "get_click_stats"
    method = "click_stats"

    class Input(BaseModel):
        days: int = Field(7, description="回看天数，默认 7")


class GetSearchAnalyticsTool(RuntimeTool):
    """读取 Google Search Console 的点击/展示/排名（按 page 维度），无凭据时返回空列表。"""

    tool_name = "get_search_analytics"
    method = "search_analytics"
    fixed = {'dimensions': ('page',)}

    class Input(BaseModel):
        days: int = Field(7, description="回看天数，默认 7")
