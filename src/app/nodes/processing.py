# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Step 2：抽字段 + 按意图执行工具，并把 retry_count 加一。"""

from __future__ import annotations

import json

from langchain_core.prompts import ChatPromptTemplate

from src.app.graph.state import WorkflowState
from src.app.services.llm_service import llm_service
from src.app.services.prompt_service import prompt_service
from src.app.services.tool_service import tool_service
from src.app.tools.base import is_retryable_error
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def processing_node(state: WorkflowState) -> WorkflowState:
    """Reads: input_text, intent, prompt_versions, requested_tools
    Writes: extracted_data, tool_results, retry_count, messages
            [明确非网络失败时另写] processing_fatal, error

    失败分两类，处理策略不同（与用户「遇异常就结束、等下次请求重试；只有网络超时
    才走重试逻辑」的取向一致）：
    - **网络/连接层瞬态故障**（LLM 调用超时 / 工具连接超时 / SourceUnavailable）→ 不短路，
      交 validation 走重试逻辑；连重试若干轮仍失败再由 validate 落 ERROR；
    - **明确非网络失败**（LLM 鉴权/配额/参数错、工具返回 ok:False 且非网络）→ 直接短路到
      ERROR（processing_fatal），不再空转重试，等调用方下次请求再试。重试对这类错误没用。
    工具级成功/失败的判定由 RuntimeTool.run 在捕获异常时按异常类型打 retryable 标记。
    """
    text = state.get("input_text", "")
    intent = state.get("intent", "general")
    versions = state.get("prompt_versions", {}) or {}
    system = prompt_service.load_prompt("extraction", version=versions.get("extraction"))
    # human 模板同样在 prompts/versions/ 下，代码里不留任何提示词字面量
    user = prompt_service.load_prompt("extraction_user",
                                     version=versions.get("extraction_user"))
    template = ChatPromptTemplate.from_messages(
        [("system", system), ("human", user)])

    extracted = llm_service.extract_fields(template=template, text=text, intent=intent)

    # LLM 故障分类：已配置 key 但调用抛异常才计失败；明确非网络失败 → 短路。
    llm_failed = bool(getattr(llm_service, "extract_failed", False))
    llm_retryable = bool(getattr(llm_service, "extract_retryable", False))
    fatal = llm_failed and not llm_retryable
    error = ""
    if llm_failed and not llm_retryable:
        error = "LLM 调用失败：已配置 API Key 但接口明确报错（非网络），不再重试"

    try:
        results = tool_service.execute_for_intent(
            intent=intent, text=text, extracted=extracted,
            tools=state.get("requested_tools"),
            prompt_versions=state.get("prompt_versions"))
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

    logger.info("processing_node", extra={"intent": intent, "tool_count": len(results), "fatal": fatal})

    # review_metrics 专属：取数后让 LLM 比对各站表现，识别「表现好的站点」。
    # 无 LLM key 时 analyze_metrics 返回空串，本段整体跳过，不阻塞整轮。
    metrics_analysis = ""
    if intent == "review_metrics" and results and not fatal:
        try:
            versions = state.get("prompt_versions", {}) or {}
            system = prompt_service.load_prompt(
                "review_metrics", version=versions.get("review_metrics"))
            metrics_analysis = llm_service.analyze_metrics(
                system, _compact_metrics_payload(results))
        except Exception:  # noqa: BLE001 - 分析失败不影响取数与出报告
            metrics_analysis = ""

    out: WorkflowState = {"extracted_data": extracted, "tool_results": results,
                          "retry_count": state.get("retry_count", 0) + 1,
                          "messages": state.get("messages", []) + ["processed"]}
    if metrics_analysis:
        out["metrics_analysis"] = metrics_analysis
    if fatal:
        out["processing_fatal"] = True
        out["error"] = error
    return out


def _compact_metrics_payload(results: dict, max_chars: int = 6000) -> str:
    """把工具结果压成 LLM 易读的精简 JSON：只取 ok 工具的 result 部分，过长截断。

    review_metrics 的数据可能很大（整张候选表 / 全站埋点），这里只保留
    get_click_stats（by_site 透视表）/ get_search_analytics / get_page_inventory
    的核心字段，避免把上报撑爆。
    """
    slim: dict = {}
    for name, out in (results or {}).items():
        if isinstance(out, dict) and out.get("ok"):
            slim[name] = out.get("result")
    try:
        text = json.dumps(slim, ensure_ascii=False, default=str)
    except TypeError:
        text = str(slim)
    if len(text) > max_chars:
        text = text[:max_chars] + f"...<truncated {len(text) - max_chars} chars>"
    return text
