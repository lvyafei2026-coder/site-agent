from src.app.graph.state import WorkflowState


def error_node(state: WorkflowState) -> WorkflowState:
    validation = state.get("validation", {})
    error = state.get("error") or validation.get("reason") or "Workflow failed validation."
    return {"output": f"ERROR: {error}", "error": error}
