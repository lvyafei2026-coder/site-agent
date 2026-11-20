# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""hotwords 域：选题雷达取数 + 热词线的取词 / 补数据 / 回写（定义 + 实现同文件）。

热词分析（POST /hotwords）与常规线（POST /run）都是「工具编排 + LLM + 落库」，
差别只在节点类型：这条线逐词回环。它用的 IO 与常规线一样是 registry 里的工具，
没有绕过工具契约的旁路。本域 6 个工具：
    get_hotwords                候选池快照（常规线 select_pages 用）
    next_candidate              取下一个未跟进的词（热词线每轮一次）
    read_examples               读人工判例（热词线每轮一次）
    fetch_serp / fetch_trend    补数据：读缓存 → 现抓 → 写回（ledger：会写缓存表）
    write_hotword_result        结论回写：正文 / 标签 / 状态（ledger：幂等 upsert）
LLM 与纯判据不是工具，编排层（services/hotword_service.py）直接复用。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from src.app.enums import CandidateStatus, ToolKind
from src.app.tools.adapters.hotwords import curation as candidates
from src.app.tools.adapters.hotwords import hotword_contract, hotword_fetch, hotword_write
from src.app.tools.base import RuntimeTool


class HotwordsOps:
    """hotwords 域的实现：只碰 hotwords 适配器。"""

    def hotwords(self, status=CandidateStatus.DOING.value, limit=40):
        return candidates.snapshot(status=status, limit=limit)

    def next_candidate(self, seen=()):
        return hotword_contract.select_pending(set(seen))

    def read_examples(self):
        return hotword_contract.read_examples()

    def fetch_serp(self, term):
        return hotword_fetch.serp(term)

    def fetch_trend(self, term):
        return hotword_fetch.trend(term)

    def write_hotword_result(self, item, text, verdict, reason="", hints=None):
        return hotword_write.write_result(item, text, hints, verdict, reason)


class GetHotwordsTool(RuntimeTool):
    """读取选题雷达候选关键词，并返回 meta（updatedMs 抓取时间、geosOk 成功区域数、raw 原始条数）。判断有无新数据必须看 meta。"""

    tool_name = "get_hotwords"
    method = "hotwords"

    class Input(BaseModel):
        status: str = Field(CandidateStatus.DOING.value, description="候选状态，默认 doing")
        limit: int = Field(40, description="最多返回几条，默认 40")


class NextCandidateTool(RuntimeTool):
    """取下一个未跟进且未分析过的热词（热词线每轮调一次）；没有更多词时 result 为 null。"""

    tool_name = "next_candidate"
    method = "next_candidate"

    class Input(BaseModel):
        seen: list[str] = Field(default_factory=list,
                                description="本轮已取过的词 key，防 KV 最终一致导致重复取词")


class ReadExamplesTool(RuntimeTool):
    """读人工「不做」标记里可喂回 AI 的判例：只人工标记、理由有泛化价值、按理由去重。"""

    tool_name = "read_examples"
    method = "read_examples"

    class Input(BaseModel):
        pass


class FetchSerpTool(RuntimeTool):
    """补 SERP 辨形证据：读 serp:v1: 缓存，缺失则现抓 DuckDuckGo 并写回缓存与竞争度表。源级不可用时 ok=False、retryable=True，编排层据此让后续词跳过该源。"""

    tool_name = "fetch_serp"
    method = "fetch_serp"
    kind = ToolKind.LEDGER.value

    class Input(BaseModel):
        term: str = Field(..., description="关键词")


class FetchTrendTool(RuntimeTool):
    """补趋势证据：读 trend:v1: 缓存，缺失则现抓 Google Trends 并写回缓存与 interests 摘要表。源级不可用时 ok=False、retryable=True。"""

    tool_name = "fetch_trend"
    method = "fetch_trend"
    kind = ToolKind.LEDGER.value

    class Input(BaseModel):
        term: str = Field(..., description="关键词")


class WriteHotwordResultTool(RuntimeTool):
    """把一个词的分析结论写回共享 KV：正文先写，成功后才写信号标签与跟进状态（做→saved/todo，不做→rejected，观望只留正文）。幂等 upsert，可重复执行。"""

    tool_name = "write_hotword_result"
    method = "write_hotword_result"
    kind = ToolKind.LEDGER.value

    class Input(BaseModel):
        item: dict[str, Any] = Field(..., description="候选词条目（term / key / related / geos / traffic…）")
        text: str = Field(..., description="LLM 分析正文")
        verdict: str = Field(..., description="结论：做 / 观望 / 不做")
        reason: str = Field("", description="判定理由（不做时写进 rejected 表）")
        hints: Optional[list[str]] = Field(None, description="信号标签；None = 正文里解析不出，不覆盖原标签")
