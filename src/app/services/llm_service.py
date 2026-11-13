# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 调用：意图分类 + 字段抽取。

无 LLM_API_KEY 时走确定性本地回退，图与测试不依赖任何外部凭据或网络。
"""

from __future__ import annotations

import json
import re

from langchain_core.prompts import ChatPromptTemplate

from src.app.config import INTENTS, LLM_API_KEY, LLM_MODEL
from src.app.llm.provider import get_llm
from src.app.observability.langsmith import traceable
from src.app.observability.metrics import metrics
from src.app.tools.base import is_retryable_error
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CONFIDENCE = 0.55


class LLMService:
    def __init__(self) -> None:
        # extract_fields 调用后写入：failed=True 表示「已配置 key 但本次调用抛异常」；
        # retryable=True 表示该异常是网络/连接层瞬态故障（应走重试），False 表示明确
        # 非网络失败（鉴权/配额/参数）。processing_node 据此决定：明确失败 → 短路到
        # ERROR 等下次请求；网络超时 → 不短路，交 validation 走重试逻辑。
        self.extract_failed: bool = False
        self.extract_retryable: bool = False

    @traceable(name="classify_intent")
    def classify_intent(self, template: ChatPromptTemplate, text: str) -> dict:
        """把请求归入 INTENTS 之一；无 key 或调用失败时回退 general。"""
        intent, confidence = "general", DEFAULT_CONFIDENCE
        raw, _, _ = self._complete(template.format(input_text=text), text, "intent_llm_failed") or (None, False, False)
        data = raw or {}
        if data:
            intent = str(data.get("intent", intent)).strip().lower()
            try:
                confidence = float(data.get("confidence", confidence))
            except (TypeError, ValueError):
                pass
        if intent not in INTENTS:
            intent = "general"
        metrics.increment("llm.classify_intent.calls")
        return {"intent": intent, "confidence": confidence}

    @traceable(name="extract_fields")
    def extract_fields(self, template: ChatPromptTemplate, text: str, intent: str) -> dict:
        """按 ExtractionSchema 的字段抽事实；LLM 不可用时用正则兜底，保证字段齐全。"""
        raw, failed, retryable = self._complete(
            template.format(input_text=text, intent=intent), text, "extract_llm_failed") or (None, False, False)
        self.extract_failed = failed
        self.extract_retryable = retryable
        data = raw or {}
        metrics.increment("llm.extract_fields.calls")
        return {
            "target_site": str(data.get("target_site", "")),
            "intent": intent,
            "keywords": list(data.get("keywords")
                             or re.findall(r"[A-Za-z][A-Za-z0-9 -]{2,30}", text))[:12],
            "pages": list(data.get("pages") or []),
            "notes": str(data.get("notes") or text[:200]),
            "requires_tool": bool(data.get("requires_tool", True)),
        }

    @staticmethod
    def _complete(prompt: str, text: str, warn_event: str) -> tuple[dict | None, bool, bool]:
        """调 OpenAI 兼容接口并解析 JSON；任何异常都降级，不阻塞整轮。

        返回 (data, failed, retryable)：
        - 无 key          → (None, False, False)：预期关闭（确定性回退），不算故障；
        - 成功            → (data, False, False)；
        - 异常且为网络层  → (None, True,  True )：连接/超时类，应走重试；
        - 异常且明确失败  → (None, True,  False)：鉴权/配额/参数，短路到 ERROR 等下次请求。
        """
        if not LLM_API_KEY:
            return None, False, False
        try:
            resp = get_llm().chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": text}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            return (data if isinstance(data, dict) else None), False, False
        except Exception as exc:  # noqa: BLE001 - LLM 不可用不影响整轮
            logger.warning(warn_event, extra={"error": str(exc)[:200]})
            return None, True, is_retryable_error(exc)


llm_service = LLMService()
