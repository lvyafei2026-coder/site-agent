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


def route_after_processing(state: WorkflowState) -> str:
    """processing 置 processing_fatal（明确的非网络失败）→ 直接 error，跳过校验与重试；
    否则（含网络/连接层瞬态故障）→ 进 validate，由 validation 走重试逻辑。

    与 route_after_validation 的分工：
    - 这里判的是「**节点级硬失败**」：LLM 鉴权/配额/参数错、工具返回明确非网络失败、
      工具抛未捕获业务异常。这些重试没用，直接结束等下次请求重试；
    - 网络超时（LLM 连接超时、工具 ConnectTimeout/SourceUnavailable）不置 fatal，
      走 validate → 重试，连重试多轮仍失败才由 validation 落 ERROR；
    - 软失败（字段偏少、部分工具 ok:False 但网络类）同样交给 validation 决定。
    """
    if state.get("processing_fatal"):
        return Tags.ERROR
    return Tags.VALIDATE


PROCESSING_EDGE_MAP = {
    Tags.VALIDATE: Names.VALIDATION,
    Tags.ERROR: Names.ERROR,
}
