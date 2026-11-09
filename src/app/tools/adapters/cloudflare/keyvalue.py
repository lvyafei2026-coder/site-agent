# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""WorkersKV：状态标记端口的 Cloudflare 实现，键统一带 agent: 前缀。"""

from __future__ import annotations

import json
from typing import Any

from .client import CloudflareClient


class WorkersKV:
    PREFIX = "agent:"

    def __init__(self, client: CloudflareClient):
        self._client = client

    def _name(self, key: str) -> str:
        return key if key.startswith(self.PREFIX) else self.PREFIX + key

    def _path(self, key: str) -> tuple[str, ...]:
        return ("storage", "kv", "namespaces", self._client.namespace_id,
                "values", self._name(key))

    def get(self, key: str, default: Any = None) -> Any:
        resp = self._client.request("GET", *self._path(key), timeout=20)
        if resp.status_code == 404:
            return default
        resp.raise_for_status()
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError:
            return resp.text

    def put(self, key: str, value: Any, ttl_sec: int | None = None) -> None:
        resp = self._client.request(
            "PUT", *self._path(key),
            params={"expiration_ttl": ttl_sec} if ttl_sec else None,
            data=json.dumps(value, ensure_ascii=False).encode(), timeout=20,
        )
        resp.raise_for_status()
