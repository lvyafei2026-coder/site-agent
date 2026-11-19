# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""hotwords 域：选题雷达取数（定义 + 实现同文件）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.app.enums import CandidateStatus, ToolKind
from src.app.tools.adapters.terms import hotwords
from src.app.tools.adapters.terms import curation as candidates
from src.app.tools.base import RuntimeTool


class HotwordsOps:
    """hotwords 域的实现：只碰 terms 适配器。"""

    def hotwords(self, status=CandidateStatus.DOING.value, limit=40):
        return candidates.snapshot(status=status, limit=limit)

    def analyze_hotwords(self, limit=20, prompt_version=None):
        # prompt_version 来自 /run 的 prompt_versions.hotwords；None 时 run_batch 走默认 v1
        return hotwords.run_batch(limit=limit, prompt_version=prompt_version)


class GetHotwordsTool(RuntimeTool):
    """读取选题雷达候选关键词，并返回 meta（updatedMs 抓取时间、geosOk 成功区域数、raw 原始条数）。判断有无新数据必须看 meta。"""

    tool_name = "get_hotwords"
    method = "hotwords"

    class Input(BaseModel):
        status: str = Field(CandidateStatus.DOING.value, description="候选状态，默认 doing")
        limit: int = Field(40, description="最多返回几条，默认 40")


class AnalyzeHotwordsTool(RuntimeTool):
    """批量分析候选池里「未跟进」的热词：补 SERP 竞争度与 30 天趋势、出站点方案，不做→不做标记，可以做→待开发。会写整理数据。"""

    tool_name = "analyze_hotwords"
    method = "analyze_hotwords"
    kind = ToolKind.WRITE.value

    class Input(BaseModel):
        limit: int = Field(20, description="一次捞取并逐条分析的条数，默认 20")
        prompt_version: str | None = Field(
            None, description="提示词版本，对应 D1 prompts 表的 hotwords/vN；留空走默认版本")
