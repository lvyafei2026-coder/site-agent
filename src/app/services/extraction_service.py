# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""字段抽取（常规线 Step 2 的共用能力）：按 ExtractionSchema 从请求里抽事实。

由 processing_service 调用；LLM 不可用时用正则兜底，保证六个契约字段齐全。
"""

from __future__ import annotations

import re

from langchain_core.prompts import ChatPromptTemplate

from src.app.enums import PromptTask
from src.app.llm.completion import complete_json
from src.app.observability.langsmith import traceable
from src.app.observability.metrics import metrics
from src.app.services.prompt_service import prompt_service


class ExtractionService:
    def __init__(self) -> None:
        # extract 调用后写入：failed=True 表示「已配置 key 但本次调用抛异常」；
        # retryable=True 表示该异常是网络/连接层瞬态故障（应走重试），False 表示明确
        # 非网络失败（鉴权/配额/参数）。processing_service 据此决定：明确失败 → 短路到
        # ERROR 等下次请求；网络超时 → 不短路，交 validation 走重试逻辑。
        self.failed: bool = False
        self.retryable: bool = False

    @traceable(name="extract_fields")
    def extract(self, text: str, intent: str, versions: dict[str, str] | None = None) -> dict:
        versions = versions or {}
        system = prompt_service.load_prompt(PromptTask.EXTRACTION.value,
                                            version=versions.get(PromptTask.EXTRACTION.value))
        # human 模板同样在 D1 的 prompts 表，代码里不留任何提示词字面量
        user = prompt_service.load_prompt(PromptTask.EXTRACTION_USER.value,
                                          version=versions.get(PromptTask.EXTRACTION_USER.value))
        template = ChatPromptTemplate.from_messages([("system", system), ("human", user)])

        data, failed, retryable = complete_json(
            template.format(input_text=text, intent=intent), text, "extract_llm_failed")
        self.failed = failed
        self.retryable = retryable
        data = data or {}
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


extraction_service = ExtractionService()