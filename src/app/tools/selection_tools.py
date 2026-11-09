# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""selection 域：选题雷达取数（定义 + 实现同文件）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.app.tools.adapters.sources import radar
from src.app.tools.base import RuntimeTool


class SelectionOps:
    """selection 域的实现：只碰 sources 适配器。"""

    def hotwords(self, status="doing", limit=40):
        return radar.snapshot(status=status, limit=limit)


class GetHotwordsTool(RuntimeTool):
    """读取选题雷达候选关键词，并返回 meta（updatedMs 抓取时间、geosOk 成功区域数、raw 原始条数）。判断有无新数据必须看 meta。"""

    tool_name = "get_hotwords"
    method = "hotwords"

    class Input(BaseModel):
        status: str = Field('doing', description="候选状态，默认 doing")
        limit: int = Field(40, description="最多返回几条，默认 40")
