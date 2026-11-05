# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""Workers KV 封装：只用于 agent: 前缀的熔断标记与审批标记。"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from ..config import env

log = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
PREFIX = "agent:"


def _base() -> str:
    return (
        f"{API}/accounts/{env('CF_ACCOUNT_ID', required=True)}"
        f"/storage/kv/namespaces/{env('KV_NAMESPACE_ID', required=True)}/values"
    )


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {env('CF_API_TOKEN', required=True)}"}


def _key(key: str) -> str:
    if not key.startswith(PREFIX):
        key = PREFIX + key
    return key


def get(key: str, default: Any = None) -> Any:
    resp = requests.get(f"{_base()}/{_key(key)}", headers=_headers(), timeout=20)
    if resp.status_code == 404:
        return default
    resp.raise_for_status()
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError:
        return resp.text


def put(key: str, value: Any, ttl_sec: int | None = None) -> None:
    params = {"expiration_ttl": ttl_sec} if ttl_sec else None
    resp = requests.put(
        f"{_base()}/{_key(key)}",
        headers=_headers(),
        params=params,
        data=json.dumps(value, ensure_ascii=False).encode(),
        timeout=20,
    )
    resp.raise_for_status()


def delete(key: str) -> None:
    resp = requests.delete(f"{_base()}/{_key(key)}", headers=_headers(), timeout=20)
    resp.raise_for_status()
