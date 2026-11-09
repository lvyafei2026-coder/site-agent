from __future__ import annotations

from src.app.graph.state import WorkflowState
from src.app.services.evaluation_service import evaluation_service
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def validation_node(state: WorkflowState) -> WorkflowState:
    """Step 3: 按字段覆盖率 + 置信度打分，决定继续、重试还是报错。

    Reads:  extracted_data, confidence, tool_results
    Writes: validation, messages
    """
    validation = evaluation_service.validate(
        extracted=state.get("extracted_data", {}),
        confidence=state.get("confidence", 0.0),
    )

    # 覆盖率之外补一条运营语义：本轮工具全失败不能算成功。
    results = state.get("tool_results", {}) or {}
    failed = [name for name, out in results.items()
              if isinstance(out, dict) and not out.get("ok", True)]
    if results and len(failed) == len(results):
        validation["is_valid"] = False
        validation["reason"] = f"本轮 {len(failed)} 个工具全部失败：{', '.join(failed)}"
    elif failed:
        validation["tool_failures"] = failed

    logger.info("validation_node", extra={"validation": validation})
    return {
        "validation": validation,
        "messages": state.get("messages", []) + ["validated"],
    }
