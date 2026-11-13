# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Step 1：判定本轮运营意图（调用方强制指定时跳过 LLM）。"""

from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

from src.app.graph.state import WorkflowState
from src.app.services.llm_service import llm_service
from src.app.services.prompt_service import prompt_service
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def intent_node(state: WorkflowState) -> WorkflowState:
    text = state.get("input_text", "")
    forced = state.get("intent", "")
    if forced:
        result = {"intent": forced, "confidence": 1.0}
    else:
        versions = state.get("prompt_versions", {}) or {}
        system = prompt_service.load_prompt("intent", version=versions.get("intent"))
        # human 模板同样在 prompts/versions/ 下，代码里不留任何提示词字面量
        user = prompt_service.load_prompt("intent_user", version=versions.get("intent_user"))
        template = ChatPromptTemplate.from_messages(
            [("system", system), ("human", user)])
        result = llm_service.classify_intent(template=template, text=text)
    logger.info("intent_node", extra={"intent": result["intent"]})
    return {"intent": result["intent"], "confidence": result["confidence"],
            "messages": state.get("messages", []) + [f"intent:{result['intent']}"]}
