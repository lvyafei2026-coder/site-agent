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
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def processing_node(state: WorkflowState) -> WorkflowState:
    """Reads: input_text, intent, prompt_versions, requested_tools
    Writes: extracted_data, tool_results, retry_count, messages
    """
    text = state.get("input_text", "")
    intent = state.get("intent", "general")
    system = prompt_service.load_prompt(
        "extraction", version=state.get("prompt_versions", {}).get("extraction"))
    template = ChatPromptTemplate.from_messages(
        [("system", system), ("human", "意图：{intent}\n请求：{input_text}")])

    extracted = llm_service.extract_fields(template=template, text=text, intent=intent)
    results = tool_service.execute_for_intent(
        intent=intent, text=text, extracted=extracted,
        tools=state.get("requested_tools"))

    logger.info("processing_node", extra={"intent": intent, "tool_count": len(results)})
    return {"extracted_data": extracted, "tool_results": results,
            "retry_count": state.get("retry_count", 0) + 1,
            "messages": state.get("messages", []) + ["processed"]}
