# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""常规线 Step 2：抽字段 + 按意图执行工具，并把 retry_count 加一。

失败分类策略（网络超时走重试 / 明确失败短路到 ERROR）在 processing_service 里。
"""

from __future__ import annotations

from src.app.enums import Intent
from src.app.graph.state import WorkflowState
from src.app.services.processing_service import processing_service
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def processing_node(state: WorkflowState) -> WorkflowState:
    """    Reads: input_text, intent, prompt_versions, requested_tools
    Writes: extracted_data, tool_results, retry_count, messages
            [明确非网络失败时另写] processing_fatal, error
            [review_metrics 分析出正文时另写] metrics_analysis, metrics_notice
            [find_backlinks 走完起稿发布时另写] outreach_result
            [word_create_page 排产出正文时另写] planning_analysis
    """
    intent = state.get("intent", Intent.GENERAL.value)
    step = processing_service.run(
        text=state.get("input_text", ""), intent=intent,
        versions=state.get("prompt_versions"),
        requested_tools=state.get("requested_tools"),
        tool_args=state.get("tool_args") or {})

    logger.info("processing_node", extra={"intent": intent,
                                          "tool_count": len(step["results"]),
                                          "fatal": step["fatal"]})

    out: WorkflowState = {"extracted_data": step["extracted"],
                          "tool_results": step["results"],
                          "retry_count": state.get("retry_count", 0) + 1,
                          "messages": state.get("messages", []) + ["processed"]}
    if step["metrics_analysis"]:
        out["metrics_analysis"] = step["metrics_analysis"]
    if step["metrics_notice"]:
        out["metrics_notice"] = step["metrics_notice"]
    if step["outreach_result"]:
        out["outreach_result"] = step["outreach_result"]
    if step["planning_analysis"]:
        out["planning_analysis"] = step["planning_analysis"]
    if step["fatal"]:
        out["processing_fatal"] = True
        out["error"] = step["error"]
    return out
