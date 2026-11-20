# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""常规线 Step 4a：校验通过，渲染最终报告（终态节点，与热词线 build_summary 对称）。"""

from src.app.graph.state import WorkflowState
from src.app.models.workflow import WorkflowResult
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def output_node(state: WorkflowState) -> WorkflowState:
    result = WorkflowResult.from_state(state)
    logger.info("output_node", extra={"status": result.status})
    return {"output": result.render()}
