# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""LangGraph 全局状态定义。

约定：
- 节点之间只通过 OpsState 传递数据，节点内部不保存跨步骤的全局变量。
- actions[].risk == "write" 的动作必须经过 interrupt() 人工确认。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

TaskType = Literal["gen-page", "daily-report", "backlink", "promo"]


class Action(TypedDict, total=False):
    type: str          # gen-page / devto-publish / content-fix / promo-draft / ...
    risk: Literal["read", "write"]
    auto: bool         # True = 白名单渠道，无需 interrupt()（仅 Dev.to）
    key: str           # 幂等键：同一 key 的动作不重复执行
    payload: dict[str, Any]
    summary: str       # 人审邮件里展示的一句话


class Check(TypedDict, total=False):
    key: str
    ok: bool
    detail: str


class OpsState(TypedDict, total=False):
    task_type: TaskType
    payload: dict[str, Any]     # 任务入参（keyword / site / date ...）
    observations: list[dict[str, Any]]
    decision: dict[str, Any]    # decide 节点的 LLM 原始输出
    actions: list[Action]
    executed: list[dict[str, Any]]
    checks: list[Check]
    result: dict[str, Any] | None
    errors: list[str]
    retry: int
