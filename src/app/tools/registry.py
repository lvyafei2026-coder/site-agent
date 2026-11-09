# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""工具装配 + 注册。

全包唯一同时认识「全部实现（6 个域 Ops）」和「全部工具（25 个类）」的模块，
所以装配放在这里：RuntimeService 把 6 个域拼成一个门面，再逐个注入给工具。
依赖方向固定为 registry → *_tools → base →（标准库 / pydantic），全程静态 import，
不需要任何延迟导入；base 不反过来认识任何域，工具也不自己去取门面。
"""

from __future__ import annotations

from typing import Any

from src.app.tools.base import BaseTool
from src.app.tools.distribution_tools import (
    DistributionOps,
    GetBacklinkLedgerTool,
    ListDevtoArticlesTool,
    PublishDevtoArticleTool,
    SearchRedditTool,
)
from src.app.tools.ledger_tools import (
    ConsumeEventTool,
    EmitEventTool,
    GetFlagTool,
    GetPendingEventsTool,
    GetReflectionsTool,
    LedgerOps,
    RecordReflectionTool,
    SendNoticeTool,
    SetFlagTool,
    WriteLedgerTool,
)
from src.app.tools.measurement_tools import (
    GetClickStatsTool,
    GetSearchAnalyticsTool,
    MeasurementOps,
)
from src.app.tools.promotion_tools import (
    GetLatestPlanTool,
    PromotionOps,
    WritePromoSlotTool,
)
from src.app.tools.publishing_tools import (
    CheckLivePageTool,
    GetBuildStatusTool,
    GetPageInventoryTool,
    GitPushTool,
    PublishingOps,
    QueueIndexingTool,
    RenderFilesTool,
    WaitBuildTool,
)
from src.app.tools.selection_tools import GetHotwordsTool, SelectionOps


class RuntimeService(SelectionOps, DistributionOps, LedgerOps,
                     MeasurementOps, PromotionOps, PublishingOps):
    """25 个工具的唯一门面：实现见各域 Ops 类，这里只有组合。"""


TOOL_CLASSES = (
    GetFlagTool,
    GetReflectionsTool,
    GetPendingEventsTool,
    WriteLedgerTool,
    SetFlagTool,
    EmitEventTool,
    RecordReflectionTool,
    ConsumeEventTool,
    SendNoticeTool,
    GetPageInventoryTool,
    GetBuildStatusTool,
    CheckLivePageTool,
    RenderFilesTool,
    WaitBuildTool,
    QueueIndexingTool,
    GitPushTool,
    GetBacklinkLedgerTool,
    SearchRedditTool,
    ListDevtoArticlesTool,
    PublishDevtoArticleTool,
    GetClickStatsTool,
    GetSearchAnalyticsTool,
    GetLatestPlanTool,
    WritePromoSlotTool,
    GetHotwordsTool,
)


class ToolRegistry:
    def __init__(self, ops: Any = None) -> None:
        self.ops: Any = None
        self._tools: dict[str, BaseTool] = {}
        self.bind(RuntimeService() if ops is None else ops)

    def bind(self, ops: Any) -> None:
        """换门面并重建工具：生产在 import 时绑一次，测试换成 FakeRuntime。"""
        self.ops = ops
        self._tools = {tool.name: tool for tool in (cls(ops) for cls in TOOL_CLASSES)}

    def get(self, name: str) -> BaseTool:
        return self._tools[name]

    def run(self, name: str, **kwargs):
        return self.get(name).run(**kwargs)

    def names(self) -> list[str]:
        return list(self._tools)

    def by_kind(self, kind: str) -> list[str]:
        return [n for n, t in self._tools.items() if t.kind == kind]


tool_registry = ToolRegistry()
