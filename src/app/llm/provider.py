# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 客户端：返回 OpenAI 兼容客户端，任何兼容服务都能接。

只负责建连接；JSON 解析、回退与打点都在 services/llm_service.py。
"""

from __future__ import annotations

import os

import httpx

from src.app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from src.app.observability.langsmith import tracing_enabled
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def content_from_response(resp) -> str | None:
    """把 OpenAI 兼容响应归一为正文 content，供所有 LLM 调用点复用。

    标准 ChatCompletion 对象、裸字符串、dict（含顶层 content）都能取到正文；
    取不到返回 None，由调用方降级，绝不抛异常把整轮带崩。
    """
    if resp is None:
        return None
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        choices = resp.get("choices")
    else:
        choices = getattr(resp, "choices", None)
    if choices:
        first = choices[0]
        if isinstance(first, str):
            return first
        msg = first.get("message") if isinstance(first, dict) else getattr(first, "message", None)
        if msg is not None:
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
            if isinstance(content, str):
                return content
    if isinstance(resp, dict):
        top = resp.get("content")
        if isinstance(top, str):
            return top
    return None


def _debug_http_client():
    """LLM_DEBUG=1 时返回带请求/响应事件钩子的 httpx 客户端，打印原始 HTTP 交换。

    能看到请求真正打到的 URL、HTTP 状态码、响应头与响应体前 600 字符，用于排查
    网关拦截、代理改写或模型返回异常内容；默认关闭，排查完把环境变量去掉即可。
    """
    if not os.getenv("LLM_DEBUG"):
        return None

    def _on_request(req: httpx.Request) -> None:
        logger.info("llm_http_request", extra={
            "method": req.method,
            "url": str(req.url),
            "body_len": len(req.content or b""),
        })

    def _on_response(res: httpx.Response) -> None:
        body = ""
        try:
            res.read()  # 钩子触发时响应体尚未读取，先强制读入再取文本
            body = (res.text or "")[:600]
        except Exception:  # noqa: BLE001 - 只读诊断，绝不干扰主流程
            body = "<unreadable>"
        logger.info("llm_http_response", extra={
            "status": res.status_code,
            "url": str(res.url),
            "headers": {k: v for k, v in res.headers.items()
                        if k.lower() not in ("authorization", "cookie", "set-cookie")},
            "body_head": body,
        })

    return httpx.Client(event_hooks={"request": [_on_request], "response": [_on_response]})


def get_llm():
    """建 OpenAI 兼容客户端；开启 LangSmith 时套一层 wrap_openai。

    wrap_openai 不改变调用方式（仍是 client.chat.completions.create），只是把
    请求体、响应、耗时、token 数作为 LLM run 上报——LLM 层 trace 靠这一层，
    不包的话 LangSmith 上只能看到节点，看不到实际发给模型的 prompt。
    """
    from openai import OpenAI

    kwargs: dict[str, object] = {"api_key": LLM_API_KEY}
    if LLM_BASE_URL:
        kwargs["base_url"] = LLM_BASE_URL
    http_client = _debug_http_client()
    if http_client is not None:
        kwargs["http_client"] = http_client
    client = OpenAI(**kwargs)
    if tracing_enabled():
        from langsmith.wrappers import wrap_openai

        client = wrap_openai(client)
    logger.info("llm_provider",
                extra={"model": LLM_MODEL, "base_url": LLM_BASE_URL,
                       "traced": tracing_enabled(), "debug_http": http_client is not None})
    return client
