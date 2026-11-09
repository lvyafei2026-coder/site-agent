# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""WorkflowState：一轮运营在节点间流动的全部字段。

字段分三组：
- 输入：由 /run 请求体注入（api/routes.py），节点只读不改；
- 中间：节点产出、供下游节点消费，最后随结果一并返回；
- 输出：终态字段，/run 返回体直接取用。

单轮流水线走向（见 graph/builder.py）：
  classify_intent → run_processing → validate_result → build_output / retry / handle_error
"""

from __future__ import annotations

from typing import Any, TypedDict

from src.app.config import settings


class WorkflowState(TypedDict, total=False):
    # ── 输入（/run 请求注入，节点只读）───────────────────────────
    input_text: str
    """必填。原始请求文本；intent 节点拿它做意图分类，processing 节点拿它做字段抽取。"""

    intent: str
    """可选输入 / 中间变量（双重身份）。
    传入时（api/routes.py 写入）= 调用方强制指定意图，intent 节点跳过 LLM 直接采用、confidence 置 1.0；
    未传入时 = intent 节点产出的中间结果，供 processing 选工具集（INTENT_TOOLS）。"""

    prompt_versions: dict[str, str]
    """可选输入。按任务指定 prompt 版本，形如 {"intent": "v1", "extraction": "v1", "validation": "v1"}；
    三个节点各取自己那份；缺省时回退 settings.default_prompt_versions（PromptRegistry 再回退 v1）。"""

    requested_tools: list[str] | None
    """可选输入。显式指定本轮调用的工具名列表，覆盖 INTENT_TOOLS 默认集。
    None = 按意图默认集（只含 read 工具）；显式传 write 工具名（如 git_push）才会触发不可逆写——这是当前唯一的写工具开关。"""

    # ── 中间变量（节点产出，节点间流动）───────────────────────────
    confidence: float
    """intent 节点产出。意图分类置信度 0~1（强制指定时为 1.0，LLM 失败回退 0.55）；
    validation 打分（confidence * 0.4）和最终 WorkflowResult 都用它。"""

    extracted_data: dict[str, Any]
    """processing 节点产出。LLM 抽取的运营字段（target_site/keywords/pages/notes 等）+ analysis 汇总；
    validation 节点以它做字段覆盖率校验。"""

    tool_results: dict[str, Any]
    """processing 节点产出。{工具名: ToolOutput dict}，入口初始化为 {}；
    validation 节点据此判断「全部失败不算 is_valid」，WorkflowResult 统计成功/失败数。"""

    validation: dict[str, Any]
    """validation 节点产出。{is_valid, reason, score, tool_failures}；
    graph/edges.py 据此路由：is_valid → build_output，否则 retry_count < 上限 → retry，再不行 → handle_error。"""

    retry_count: int
    """循环控制。入口置 0，processing 节点每执行一轮 +1；
    edges 与 settings.max_validation_retries（默认 2）比较，决定还允许重试几次。"""

    messages: list[str]
    """过程轨迹。入口置 []，各节点追加标记（intent:xxx / processed / validated）；
    无节点消费，仅随最终结果返回，用于排查一轮里实际走了哪些节点。"""

    # ── 输出（终态，/run 返回体直接取用）───────────────────────────
    output: str
    """终态文本。build_output 节点写入 WorkflowResult.render() 的运营报告；
    handle_error 节点写入 "ERROR: <原因>"。/run 返回体里的 output 字段就是它。"""

    error: str
    """终态错误信息。handle_error 节点写入失败原因（优先取 validation["reason"]）；
    edges 兜底：validate_result 之后若 error 非空，直接路由到 handle_error。"""


def initial_state(
    input_text: str,
    *,
    intent: str | None = None,
    tools: list[str] | None = None,
    prompt_versions: dict[str, str] | None = None,
) -> WorkflowState:
    """构造一轮的入口状态。CLI 与 /run 共用，避免两处各写一份默认值。"""
    state: WorkflowState = {
        "input_text": input_text,
        "retry_count": 0,
        "messages": [],
        "prompt_versions": dict(prompt_versions or settings.default_prompt_versions),
        "tool_results": {},
        "requested_tools": tools,
    }
    if intent:
        state["intent"] = intent
    return state
