# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Step 2：抽字段 + 按意图执行工具，并把 retry_count 加一。"""

from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

from src.app.graph.state import WorkflowState
from src.app.services.llm_service import llm_service
from src.app.services.prompt_service import prompt_service
from src.app.services.tool_service import tool_service
from src.app.tools.base import is_retryable_error
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def processing_node(state: WorkflowState) -> WorkflowState:
    """Reads: input_text, intent, prompt_versions, requested_tools
    Writes: extracted_data, tool_results, retry_count, messages
            [明确非网络失败时另写] processing_fatal, error

    失败分两类，处理策略不同（与用户「遇异常就结束、等下次请求重试；只有网络超时
    才走重试逻辑」的取向一致）：
    - **网络/连接层瞬态故障**（LLM 调用超时 / 工具连接超时 / SourceUnavailable）→ 不短路，
      交 validation 走重试逻辑；连重试若干轮仍失败再由 validate 落 ERROR；
    - **明确非网络失败**（LLM 鉴权/配额/参数错、工具返回 ok:False 且非网络）→ 直接短路到
      ERROR（processing_fatal），不再空转重试，等调用方下次请求再试。重试对这类错误没用。
    工具级成功/失败的判定由 RuntimeTool.run 在捕获异常时按异常类型打 retryable 标记。
    """
    text = state.get("input_text", "")
    intent = state.get("intent", "general")
    system = prompt_service.load_prompt(
        "extraction", version=state.get("prompt_versions", {}).get("extraction"))
    template = ChatPromptTemplate.from_messages(
        [("system", system), ("human", "意图：{intent}\n请求：{input_text}")])

    extracted = llm_service.extract_fields(template=template, text=text, intent=intent)

    # LLM 故障分类：已配置 key 但调用抛异常才计失败；明确非网络失败 → 短路。
    llm_failed = bool(getattr(llm_service, "extract_failed", False))
    llm_retryable = bool(getattr(llm_service, "extract_retryable", False))
    fatal = llm_failed and not llm_retryable
    error = ""
    if llm_failed and not llm_retryable:
        error = "LLM 调用失败：已配置 API Key 但接口明确报错（非网络），不再重试"

    try:
        results = tool_service.execute_for_intent(
            intent=intent, text=text, extracted=extracted,
            tools=state.get("requested_tools"))
    except Exception as exc:  # noqa: BLE001 - 工具抛未捕获异常（非 RuntimeTool 安全网）
        # 只有「明确非网络失败」才短路；网络超时类交给 validation 重试。
        if not is_retryable_error(exc):
            fatal = True
            error = f"processing 执行工具时抛异常：{str(exc)[:300]}"
        results = {}

    # 工具级失败分类：任一「明确非网络失败」→ 短路结束，等下次请求重试；
    # 仅「网络超时」类（retryable=True）才允许走 validate → 重试逻辑。
    if not fatal:
        for name, res in results.items():
            if isinstance(res, dict) and res.get("ok") is False and not res.get("retryable", False):
                fatal = True
                error = f"工具 {name} 明确失败（非网络）：{str(res.get('error', ''))[:200]}"
                break

    logger.info("processing_node", extra={"intent": intent, "tool_count": len(results), "fatal": fatal})
    out: WorkflowState = {"extracted_data": extracted, "tool_results": results,
                          "retry_count": state.get("retry_count", 0) + 1,
                          "messages": state.get("messages", []) + ["processed"]}
    if fatal:
        out["processing_fatal"] = True
        out["error"] = error
    return out
