from src.app.config import settings
from src.app.graph.constants import Names, Tags
from src.app.graph.state import WorkflowState


def route_after_validation(state: WorkflowState) -> str:
    """校验通过 → build_output；还能重试 → run_processing；否则 → handle_error。"""
    if state.get("validation", {}).get("is_valid"):
        return Tags.COMPLETE
    if state.get("retry_count", 0) < settings.max_validation_retries:
        return Tags.RETRY
    return Tags.ERROR


EDGE_MAP = {
    Tags.COMPLETE: Names.OUTPUT,
    Tags.RETRY: Names.PROCESSING,
    Tags.ERROR: Names.ERROR,
}
