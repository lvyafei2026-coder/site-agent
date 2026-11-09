# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 客户端：返回 OpenAI 兼容客户端，任何兼容服务都能接。

只负责建连接；JSON 解析、回退与打点都在 services/llm_service.py。
"""

from __future__ import annotations

from src.app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def get_llm():
    from openai import OpenAI

    kwargs: dict[str, str] = {"api_key": LLM_API_KEY}
    if LLM_BASE_URL:
        kwargs["base_url"] = LLM_BASE_URL
    logger.info("llm_provider", extra={"model": LLM_MODEL, "base_url": LLM_BASE_URL})
    return OpenAI(**kwargs)
