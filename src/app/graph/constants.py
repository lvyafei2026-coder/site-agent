"""图装配的节点名与路由标签。

节点名刻意用动词前缀：langgraph 0.3.x 不允许节点名与 WorkflowState 的字段名
同名，否则 add_node 抛 "'intent' is already being used as a state key"。
"""


class Names:
    INTENT = "classify_intent"
    PROCESSING = "run_processing"
    VALIDATION = "validate_result"
    OUTPUT = "build_output"
    ERROR = "handle_error"


class Tags:
    RETRY = "retry"
    COMPLETE = "complete"
    ERROR = "error"
