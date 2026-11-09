"""Step 4a：校验通过，渲染最终报告。"""

from src.app.graph.state import WorkflowState
from src.app.models.workflow import WorkflowResult
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def output_node(state: WorkflowState) -> WorkflowState:
    result = WorkflowResult.from_state(state)
    logger.info("output_node", extra={"status": result.status})
    return {"output": result.render()}
