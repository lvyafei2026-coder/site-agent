# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 额度查询：发一次最小推理请求，读回 x-ratelimit-* 响应头。

多数 OpenAI 兼容网关没有统一的「余额」面板，剩余额度/限流信息只在响应头里。
这里用 max_tokens=1 的 ping 把头部带回来，成本是 1 次请求，只在页面上手动
点击时触发。

不走 openai SDK 的 with_raw_response：部分网关会裁剪响应头，直接 httpx
请求可拿全量头；429 时头里带 retry-after 与剩余量，同样有价值，一并返回。
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from src.app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

# 与 LLM_MODEL 默认值 deepseek-flash 对齐：LLM_BASE_URL 没显式配时，ping 也打 DeepSeek，
# 而不是 OpenAI（否则模型名 deepseek-flash 在 OpenAI 会被拒）。显式配 LLM_BASE_URL 时永远优先。
_DEFAULT_BASE = "https://api.deepseek.com"


def _is_deepseek() -> bool:
    """是否命中 DeepSeek：base_url 显含 deepseek，或未配 base_url 且模型以 deepseek 开头。

    命中时 ping 要显式关掉思考模式（见 _thinking_off），否则 max_tokens=1 会被 reasoning 吃光。
    """
    base = (LLM_BASE_URL or "").lower()
    if "deepseek" in base:
        return True
    if not LLM_BASE_URL and LLM_MODEL.lower().startswith("deepseek"):
        return True
    return False


def _thinking_off() -> dict:
    """DeepSeek 思考模式默认开启：reasoning_content 会先吃掉 max_tokens，
    导致 max_tokens=1 的 ping 返回空正文（虽不影响 quota 头读取，但浪费且不稳）。
    与逐词分析 `_thinking_kwargs` 一致，这里也显式关掉。
    DeepSeek 接受顶层 `thinking` 字段，与 OpenAI SDK 的 extra_body 等价。
    """
    return {"thinking": {"type": "disabled"}} if _is_deepseek() else {}


def check_quota() -> dict:
    """返回 {ok, model, endpoint, quota{header:value}, checked_at}；失败时 ok=false。"""
    if not LLM_API_KEY:
        return {"ok": False, "reason": "LLM_API_KEY 未配置，无法查询额度"}

    base = (LLM_BASE_URL or _DEFAULT_BASE).rstrip("/")
    url = base + "/chat/completions"
    payload = {"model": LLM_MODEL,
               "messages": [{"role": "user", "content": "ping"}],
               "max_tokens": 1}
    payload.update(_thinking_off())  # DeepSeek 时带 thinking:disabled；其它网关忽略该字段
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {LLM_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=20.0,
        )
    except Exception as exc:  # 网络 / DNS 归为查询失败，不抛出
        return {"ok": False, "reason": f"额度查询请求失败：{exc}"}

    headers = {k.lower(): v for k, v in resp.headers.items()}
    quota = {k: v for k, v in headers.items()
             if "ratelimit" in k or k == "retry-after"}

    if resp.status_code == 429:          # 被限流：头里正好带着剩余量与等待秒数
        return {"ok": True, "model": LLM_MODEL, "endpoint": urlparse(base).netloc,
                "quota": quota, "limited": True, "checked_at": _now()}
    if resp.status_code >= 400:          # 其它错误（key 无效 / 模型名错等）
        detail = ""
        try:
            body = resp.json()
            detail = str(body.get("error", {}).get("message", ""))[:200]
        except (ValueError, AttributeError):
            detail = resp.text[:200]
        return {"ok": False,
                "reason": f"额度查询返回 HTTP {resp.status_code}：{detail or '无详情'}"}

    # 部分网关 200 响应不带 x-ratelimit-* 头（只在 429 时带 retry-after），
    # 所以正常时 quota 为空属预期
    return {"ok": True, "model": LLM_MODEL, "endpoint": urlparse(base).netloc,
            "quota": quota, "available": True, "checked_at": _now()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
