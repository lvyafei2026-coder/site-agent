# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""意图识别（nodes/normal_intent_node.py 的实现类）：把请求归入 INTENTS 之一。

提示词从 D1 经 prompt_service 取，补全与降级走 llm/completion.py 共用底座。
无 key 或调用失败时回退 general，图不依赖任何外部凭据或网络。
"""

from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

from src.app.config import INTENTS
from src.app.enums import Intent, PromptTask
from src.app.llm.completion import complete_json
from src.app.observability.langsmith import traceable
from src.app.observability.metrics import metrics
from src.app.services.prompt_service import prompt_service

DEFAULT_CONFIDENCE = 0.55


class IntentService:
    @traceable(name="classify_intent")
    def classify(self, text: str, versions: dict[str, str] | None = None) -> dict:
        """返回 {intent, confidence}；模型给的意图不在枚举里一律落 general。"""
        versions = versions or {}
        system = prompt_service.load_prompt(PromptTask.INTENT.value,
                                            version=versions.get(PromptTask.INTENT.value))
        # human 模板同样在 D1 的 prompts 表，代码里不留任何提示词字面量
        user = prompt_service.load_prompt(PromptTask.INTENT_USER.value,
                                          version=versions.get(PromptTask.INTENT_USER.value))
        template = ChatPromptTemplate.from_messages([("system", system), ("human", user)])

        intent, confidence = Intent.GENERAL.value, DEFAULT_CONFIDENCE
        data, _, _ = complete_json(template.format(input_text=text), text, "intent_llm_failed")
        if data:
            intent = str(data.get("intent", intent)).strip().lower()
            try:
                confidence = float(data.get("confidence", confidence))
            except (TypeError, ValueError):
                pass
        if intent not in INTENTS:
            intent = Intent.GENERAL.value
        metrics.increment("llm.classify_intent.calls")
        return {"intent": intent, "confidence": confidence}


intent_service = IntentService()