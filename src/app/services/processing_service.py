# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""常规线 Step 2 的实现类（nodes/normal_processing_node.py）：抽字段 + 按意图取数。

组合四个共用能力：extraction_service（抽字段）、tool_service（工具取数）、
metrics_service（review_metrics 专属分析 + 邮件推送）、outreach_service
（find_backlinks 专属起稿 + Dev.to 发布），并持有本步的失败分类策略。

失败分两类，处理策略不同（与「遇异常就结束、等下次请求重试；只有网络超时才走
重试逻辑」的取向一致）：
- **网络/连接层瞬态故障**（LLM 调用超时 / 工具连接超时 / SourceUnavailable）→ 不短路，
  交 validation 走重试逻辑；连重试若干轮仍失败再由 validate 落 ERROR；
- **明确非网络失败**（LLM 鉴权/配额/参数错、工具返回 ok:False 且非网络）→ 直接短路到
  ERROR（fatal），不再空转重试，等调用方下次请求再试。重试对这类错误没用。
工具级成功/失败的判定由 RuntimeTool.run 在捕获异常时按异常类型打 retryable 标记。
"""

from __future__ import annotations

from src.app.enums import Intent
from src.app.services.extraction_service import extraction_service
from src.app.services.metrics_service import metrics_service
from src.app.services.outreach_service import outreach_service
from src.app.services.planning_service import planning_service
from src.app.services.promotion_planning_service import promotion_planning_service
from src.app.services.tool_service import tool_service
from src.app.tools.base import is_retryable_error


class ProcessingService:
    def run(self, text: str, intent: str, versions: dict[str, str] | None = None,
            requested_tools: list[str] | None = None,
            tool_args: dict[str, dict] | None = None) -> dict:
        """返回 {extracted, results, fatal, error, metrics_analysis, metrics_notice,
        outreach_result, planning_analysis, promotion_analysis}，由节点写回 state。"""
        extracted = extraction_service.extract(text, intent, versions)

        # LLM 故障分类：已配置 key 但调用抛异常才计失败；明确非网络失败 → 短路。
        fatal = extraction_service.failed and not extraction_service.retryable
        error = ""
        if fatal:
            error = "LLM 调用失败：已配置 API Key 但接口明确报错（非网络），不再重试"

        try:
            results = tool_service.execute_for_intent(
                intent=intent, text=text, extracted=extracted,
                tools=requested_tools, tool_args=tool_args,
                prompt_versions=versions)
        except Exception as exc:  # noqa: BLE001 - 工具抛未捕获异常（非 RuntimeTool 安全网）
            # 只有「明确非网络失败」才短路；网络超时类交给 validation 重试。
            if not is_retryable_error(exc):
                fatal = True
                error = f"processing 执行工具时抛异常：{str(exc)[:300]}"
            results = {}

        # 工具级失败分类：任一「明确非网络失败」→ 短路结束，等下次请求重试；
        # 仅「网络超时」类（retryable=True）才允许走 validate → 重试逻辑。
        if not fatal:
            for name, res in results.items():
                if isinstance(res, dict) and res.get("ok") is False and not res.get("retryable", False):
                    fatal = True
                    error = f"工具 {name} 明确失败（非网络）：{str(res.get('error', ''))[:200]}"
                    break

        # review_metrics 专属：取数后让 LLM 比对各站表现，识别「表现好的站点」。
        # 无 LLM key 时 analyze 返回空串，本段整体跳过，不阻塞整轮。
        metrics_analysis = ""
        metrics_notice: dict = {}
        if intent == Intent.REVIEW_METRICS.value and results and not fatal:
            try:
                metrics_analysis = metrics_service.analyze(results, versions)
            except Exception:  # noqa: BLE001 - 分析失败不影响取数与出报告
                metrics_analysis = ""
            if metrics_analysis:
                try:
                    metrics_notice = metrics_service.notify(metrics_analysis)
                except Exception as exc:  # noqa: BLE001 - 邮件失败只记录，不阻塞出报告
                    metrics_notice = {"sent": False, "detail": str(exc)[:200]}

        # find_backlinks 专属：取数（机会去重 + Reddit + Dev.to 热门）后 LLM 起稿
        # 推广文章并发布。起稿 / 发布失败只如实记录，不阻塞整轮。
        outreach_result: dict = {}
        if intent == Intent.FIND_BACKLINKS.value and results and not fatal:
            try:
                outreach_result = outreach_service.run(results, extracted, versions)
            except Exception:  # noqa: BLE001 - 推广失败不影响出报告
                outreach_result = {"ok": False, "status": "failed", "detail": "外链流程异常"}

        # word_plan_page 专属：三份取数（待开发池 / 近 30 天新页 / 历史复盘）交给
        # LLM 出排产方案。分析失败返回空串，不影响取数与出报告。
        planning_analysis = ""
        if intent == Intent.WORD_PLAN_PAGE.value and results and not fatal:
            try:
                planning_analysis = planning_service.plan(results, versions)
            except Exception:  # noqa: BLE001 - 排产失败不影响取数与出报告
                planning_analysis = ""

        # plan_promo 专属：三份取数（上版排期 / 近 30 天新页 / 待消费事件）→
        # 确定性排期 + 落库 + 代码渲染报告。与 word_plan_page 一样：失败只空串，
        # 不阻塞取数与出报告；落库是流程自动化的产物，不由 LLM 决定写不写。
        promotion_analysis = ""
        if intent == Intent.PLAN_PROMO.value and results and not fatal:
            try:
                promotion_analysis = promotion_planning_service.plan(results)
            except Exception:  # noqa: BLE001 - 排期失败不影响取数与出报告
                promotion_analysis = ""

        return {"extracted": extracted, "results": results, "fatal": fatal,
                "error": error, "metrics_analysis": metrics_analysis,
                "metrics_notice": metrics_notice, "outreach_result": outreach_result,
                "planning_analysis": planning_analysis,
                "promotion_analysis": promotion_analysis}


processing_service = ProcessingService()
