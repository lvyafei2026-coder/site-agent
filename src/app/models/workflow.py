# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""一轮运营的最终产物：状态、校验原因、工具结果与错误摘要。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.app.enums import Intent, WorkflowStatus


class WorkflowResult(BaseModel):
    intent: str
    confidence: float
    status: str
    validation_reason: str
    tool_results: dict = Field(default_factory=dict)
    tool_errors: list[str] = Field(default_factory=list)
    metrics_analysis: str = Field(default="")
    metrics_notice: dict = Field(default_factory=dict)
    outreach_result: dict = Field(default_factory=dict)
    planning_analysis: str = Field(default="")
    promotion_analysis: str = Field(default="")

    @classmethod
    def from_state(cls, state: dict) -> "WorkflowResult":
        validation = state.get("validation", {})
        results = state.get("tool_results", {}) or {}
        errors = [f"{k}: {v.get('error')}" for k, v in results.items()
                  if isinstance(v, dict) and not v.get("ok", True)]
        return cls(
            intent=state.get("intent", Intent.GENERAL.value),
            confidence=float(state.get("confidence", 0.0)),
            status=(WorkflowStatus.SUCCESS.value if validation.get("is_valid")
                    else WorkflowStatus.FAILED.value),
            validation_reason=validation.get("reason", "No validation result."),
            tool_results=results,
            tool_errors=errors,
            metrics_analysis=state.get("metrics_analysis", "") or "",
            metrics_notice=state.get("metrics_notice", {}) or {},
            outreach_result=state.get("outreach_result", {}) or {},
            planning_analysis=state.get("planning_analysis", "") or "",
            promotion_analysis=state.get("promotion_analysis", "") or "",
        )

    def render(self) -> str:
        base = (f"intent={self.intent}; confidence={self.confidence:.2f}; "
                f"status={self.status}; validation={self.validation_reason}; "
                f"tools={len(self.tool_results)}; errors={len(self.tool_errors)}")
        if self.metrics_analysis:
            base += "\n\n" + self.metrics_analysis
        if self.metrics_notice:
            base += f"\n\n[复盘邮件] sent={self.metrics_notice.get('sent')} " \
                    f"subject={self.metrics_notice.get('subject', '')}"
        if self.outreach_result:
            article = self.outreach_result.get("article") or {}
            base += f"\n\n[外链发布] ok={self.outreach_result.get('ok')} " \
                    f"status={self.outreach_result.get('status', '')} " \
                    f"source={self.outreach_result.get('source_url', '')} " \
                    f"target={article.get('target_url', '')}"
        if self.planning_analysis:
            base += "\n\n" + self.planning_analysis
        if self.promotion_analysis:
            base += "\n\n" + self.promotion_analysis
        return base
