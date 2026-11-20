# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""常规线 Step 3：按字段覆盖率 + 置信度打分，决定继续、重试还是报错。"""

from __future__ import annotations

from src.app.graph.state import WorkflowState
from src.app.services.validation_service import validation_service
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def validation_node(state: WorkflowState) -> WorkflowState:
    """Reads:  extracted_data, confidence, tool_results
    Writes: validation, messages
    """
    validation = validation_service.validate(
        extracted=state.get("extracted_data", {}),
        confidence=state.get("confidence", 0.0),
        tool_results=state.get("tool_results", {}) or {},
    )
    logger.info("validation_node", extra={"validation": validation})
    return {
        "validation": validation,
        "messages": state.get("messages", []) + ["validated"],
    }
