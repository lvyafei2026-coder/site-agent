# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""hotwords 域：选题雷达取数（定义 + 实现同文件）。

2026-11-20 分层归位：`analyze_hotwords` 不是工具而是编排（invoke 循环图），已上移到
graph 层，用户线由 api/routes.py 入口分流、后台线由 main.py 驱动。本域只保留
`get_hotwords` 这一个纯读工具。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.app.enums import CandidateStatus
from src.app.tools.adapters.terms import curation as candidates
from src.app.tools.base import RuntimeTool


class HotwordsOps:
    """hotwords 域的实现：只碰 terms 适配器。"""

    def hotwords(self, status=CandidateStatus.DOING.value, limit=40):
        return candidates.snapshot(status=status, limit=limit)


class GetHotwordsTool(RuntimeTool):
    """读取选题雷达候选关键词，并返回 meta（updatedMs 抓取时间、geosOk 成功区域数、raw 原始条数）。判断有无新数据必须看 meta。"""

    tool_name = "get_hotwords"
    method = "hotwords"

    class Input(BaseModel):
        status: str = Field(CandidateStatus.DOING.value, description="候选状态，默认 doing")
        limit: int = Field(40, description="最多返回几条，默认 40")
