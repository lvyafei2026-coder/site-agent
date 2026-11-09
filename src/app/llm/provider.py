# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 客户端：返回 OpenAI 兼容客户端，任何兼容服务都能接。

只负责建连接；JSON 解析、回退与打点都在 services/llm_service.py。
"""

from __future__ import annotations

from src.app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from src.app.observability.langsmith import tracing_enabled
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def get_llm():
    """建 OpenAI 兼容客户端；开启 LangSmith 时套一层 wrap_openai。

    wrap_openai 不改变调用方式（仍是 client.chat.completions.create），只是把
    请求体、响应、耗时、token 数作为 LLM run 上报——LLM 层 trace 靠这一层，
    不包的话 LangSmith 上只能看到节点，看不到实际发给模型的 prompt。
    """
    from openai import OpenAI

    kwargs: dict[str, str] = {"api_key": LLM_API_KEY}
    if LLM_BASE_URL:
        kwargs["base_url"] = LLM_BASE_URL
    client = OpenAI(**kwargs)
    if tracing_enabled():
        from langsmith.wrappers import wrap_openai

        client = wrap_openai(client)
    logger.info("llm_provider",
                extra={"model": LLM_MODEL, "base_url": LLM_BASE_URL,
                       "traced": tracing_enabled()})
    return client
