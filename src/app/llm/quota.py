# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 额度查询：发一次最小推理请求，读回 x-ratelimit-* 响应头。

GitHub Models（以及多数 OpenAI 兼容网关）没有"余额"面板，剩余额度只在
每次推理响应的头部里。这里用 max_tokens=1 的 ping 把头部带回来，成本是
1 次请求额度（Low 档日配额 150 次里的 1 次），只在页面上手动点击时触发。
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from src.app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

# 只透传这些头，避免把上游全部响应头（含内部信息）暴露给页面
_QUOTA_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-used-requests",
    "x-ratelimit-used-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-resource",
    "retry-after",
)


def check_quota() -> dict:
    """返回 {ok, model, endpoint, quota{header:value}, checked_at}；失败时 ok=false。"""
    if not LLM_API_KEY:
        return {"ok": False, "reason": "LLM_API_KEY 未配置，无法查询额度"}

    from openai import OpenAI

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None,
                    max_retries=0, timeout=20.0)
    try:
        raw = client.chat.completions.with_raw_response.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
    except Exception as exc:  # 网络 / 429 / 模型名错都归为查询失败，不抛出
        return {"ok": False, "reason": f"额度查询请求失败：{exc}"}

    headers = {k.lower(): v for k, v in raw.headers.items()}
    quota = {k: headers[k] for k in _QUOTA_HEADERS if k in headers}
    host = urlparse(LLM_BASE_URL).netloc if LLM_BASE_URL else "api.openai.com"
    return {
        "ok": True,
        "model": LLM_MODEL,
        "endpoint": host,
        "quota": quota,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
