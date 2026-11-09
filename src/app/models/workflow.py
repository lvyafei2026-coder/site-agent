# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""一轮运营的最终产物：状态、校验原因、工具结果与错误摘要。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkflowResult(BaseModel):
    intent: str
    confidence: float
    status: str
    validation_reason: str
    tool_results: dict = Field(default_factory=dict)
    tool_errors: list[str] = Field(default_factory=list)

    @classmethod
    def from_state(cls, state: dict) -> "WorkflowResult":
        validation = state.get("validation", {})
        results = state.get("tool_results", {}) or {}
        errors = [f"{k}: {v.get('error')}" for k, v in results.items()
                  if isinstance(v, dict) and not v.get("ok", True)]
        return cls(
            intent=state.get("intent", "general"),
            confidence=float(state.get("confidence", 0.0)),
            status="success" if validation.get("is_valid") else "failed",
            validation_reason=validation.get("reason", "No validation result."),
            tool_results=results,
            tool_errors=errors,
        )

    def render(self) -> str:
        return (f"intent={self.intent}; confidence={self.confidence:.2f}; "
                f"status={self.status}; validation={self.validation_reason}; "
                f"tools={len(self.tool_results)}; errors={len(self.tool_errors)}")
