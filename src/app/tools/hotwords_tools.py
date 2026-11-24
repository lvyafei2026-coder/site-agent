# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""hotwords 域：选题雷达取数 + 热词线的取词 / 补数据 / 回写（定义 + 实现同文件）。

热词分析（POST /hotwords）与常规线（POST /run）都是「工具编排 + LLM + 落库」，
差别只在节点类型：这条线逐词回环。它用的 IO 与常规线一样是 registry 里的工具，
没有绕过工具契约的旁路。本域 6 个工具：
    get_hotwords                候选池快照（常规线 word_plan_page 用）
    next_candidate              取下一个未跟进的词（热词线每轮一次）
    read_examples               读人工判例（热词线每轮一次）
    fetch_serp / fetch_trend    补证据：读缓存 → 现抓 → 写回（ledger：会写缓存表）
    write_hotword_result        后置动作：正文 / 标签 / 状态回写（ledger：幂等 upsert）
本域 5 个逐词工具与逐词子图的步骤节点一一对应，对应关系只写在
services/tool_service.py 的 STEP_TOOLS（步骤名 → 工具名），节点里没有工具名字面量。
LLM 与纯判据不是工具，节点层直接复用。
「本轮走哪几步补证据、哪几个后置动作」由 graph/hotword_graph.py 的名单连线决定——
加一个证据源 = 本文件加工具 + nodes/ 加节点 + enums 加步骤名 + 两张表各加一行。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from src.app.enums import CandidateStatus, ToolKind
from src.app.tools.adapters.hotwords import fetch, wordlib, writeback
from src.app.tools.base import RuntimeTool


class HotwordsOps:
    """hotwords 域的实现：只碰 hotwords 适配器。"""

    def hotwords(self, status=CandidateStatus.DOING.value, limit=40):
        return wordlib.snapshot(status=status, limit=limit)

    def next_candidate(self, seen=()):
        return wordlib.select_pending(set(seen))

    def read_examples(self):
        return wordlib.read_examples()

    def fetch_serp(self, term):
        return fetch.serp(term)

    def fetch_trend(self, term):
        return fetch.trend(term)

    def write_hotword_result(self, item, text, verdict, reason="", hints=None):
        return writeback.write_result(item, text, hints, verdict, reason)


class GetHotwordsTool(RuntimeTool):
    """读取选题雷达候选关键词，并返回 meta（updatedMs 抓取时间、geosOk 成功区域数、raw 原始条数）。判断有无新数据必须看 meta。doing=未跟进（candidates）；todo=待开发（saved 表，热词分析判「做」的产出，word_plan_page 排产用）。"""

    tool_name = "get_hotwords"
    method = "hotwords"

    class Input(BaseModel):
        status: str = Field(CandidateStatus.DOING.value,
                            description="doing=未跟进（candidates 投影）；todo=待开发（saved 表过滤）")
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
