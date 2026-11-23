# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""常规线 Step 1：判定本轮运营意图（调用方强制指定时跳过 LLM）。"""

from __future__ import annotations

from src.app.enums import normalize_intent
from src.app.graph.state import WorkflowState
from src.app.services.intent_service import intent_service
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def intent_node(state: WorkflowState) -> WorkflowState:
    """Reads: input_text, intent, prompt_versions
    Writes: intent, confidence, messages
    """
    forced = normalize_intent(state.get("intent", ""))
    if forced:
        result = {"intent": forced, "confidence": 1.0}
    else:
        result = intent_service.classify(state.get("input_text", ""),
                                         state.get("prompt_versions"))
    logger.info("intent_node", extra={"intent": result["intent"]})
    return {"intent": result["intent"], "confidence": result["confidence"],
            "messages": state.get("messages", []) + [f"intent:{result['intent']}"]}
