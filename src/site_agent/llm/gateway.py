# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""LLM 网关：按任务配置模型与温度，统一输出 JSON，内置熔断。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from ..cf import kv
from .breaker import ensure_alive, record

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"

PROFILES: dict[str, dict[str, Any]] = {
    "content-gen": {"model": "gpt-4o-mini", "temperature": 0.7, "max_tokens": 4000},
    "data-report": {"model": "gpt-4o-mini", "temperature": 0.2, "max_tokens": 2000},
    "backlink-score": {"model": "gpt-4o-mini", "temperature": 0.3, "max_tokens": 1200},
    "promo-plan": {"model": "gpt-4o-mini", "temperature": 0.6, "max_tokens": 2500},
}

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {"api_key": os.environ["LLM_API_KEY"]}
        base = os.environ.get("LLM_BASE_URL")
        if base:
            kwargs["base_url"] = base
        _client = OpenAI(**kwargs)
    return _client


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def json_call(profile: str, system: str, user: str, retries: int = 2) -> dict[str, Any]:
    """调用 LLM，强制 JSON 输出。失败重试 + 熔断计数。"""
    ensure_alive("llm")
    cfg = PROFILES[profile]
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client().chat.completions.create(
                model=cfg["model"],
                temperature=cfg["temperature"],
                max_tokens=cfg["max_tokens"],
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            usage = getattr(resp, "usage", None)
            if usage:
                log.info(
                    "llm profile=%s tokens in=%s out=%s",
                    profile, usage.prompt_tokens, usage.completion_tokens,
                )
            record("llm", ok=True)
            try:
                bump_usage()
            except Exception:  # noqa: BLE001 - 用量统计失败不影响主流程
                log.warning("LLM 用量计数写入失败")
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as exc:  # noqa: BLE001 - 统一计入熔断
            last_error = exc
            log.warning("llm 调用失败(%s/%s): %s", attempt + 1, retries + 1, exc)
            record("llm", ok=False, error=str(exc))
    raise RuntimeError(f"LLM 调用失败: {last_error}")


def usage_today() -> int:
    """当日调用计数（存在 KV，供日报展示与限额判断）。"""
    return int(kv.get("llm:calls", 0) or 0)


def bump_usage() -> int:
    count = usage_today() + 1
    kv.put("llm:calls", count, ttl_sec=86400 * 2)
    return count
