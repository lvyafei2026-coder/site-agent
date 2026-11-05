# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""D1 HTTP API 封装（不引入 wrangler，纯 REST）。"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..config import env

log = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
TIMEOUT = 30


def _url() -> str:
    return (
        f"{API}/accounts/{env('CF_ACCOUNT_ID', required=True)}"
        f"/d1/database/{env('D1_DATABASE_ID', required=True)}/query"
    )


def query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    resp = requests.post(
        _url(),
        headers={"Authorization": f"Bearer {env('CF_API_TOKEN', required=True)}"},
        json={"sql": sql, "params": params or []},
        timeout=TIMEOUT,
    )
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"D1 error: {body.get('errors')}")
    result = body.get("result") or []
    return result[0].get("results", []) if result else []


def execute(sql: str, params: list[Any] | None = None) -> None:
    """写操作。约定：Agent 不执行 DELETE。"""
    lowered = sql.strip().lower()
    if lowered.startswith("delete"):
        raise PermissionError("Agent 不允许对 D1 执行 DELETE（见设计文档 §7）")
    query(sql, params)
