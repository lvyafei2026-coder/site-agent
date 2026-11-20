# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""常规线 Step 4b：终态错误处理，把失败原因写进 output / error。"""

from src.app.graph.state import WorkflowState


def error_node(state: WorkflowState) -> WorkflowState:
    validation = state.get("validation", {})
    error = state.get("error") or validation.get("reason") or "Workflow failed validation."
    return {"output": f"ERROR: {error}", "error": error}
