# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""抽取结果的数据契约：六个字段决定覆盖率与工具参数。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractionSchema(BaseModel):
    """processing_node 从一轮运营请求里抽出的结构化事实。"""

    target_site: str = Field(default="", description="目标子站名，空表示全站群")
    intent: str = Field(default="general", description="本轮意图")
    keywords: list[str] = Field(default_factory=list, description="候选热词 / 关键词")
    pages: list[str] = Field(default_factory=list, description="相关页面 slug")
    notes: str = Field(default="", description="补充约束")
    requires_tool: bool = Field(default=True, description="是否需要执行工具")
