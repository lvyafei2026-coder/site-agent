# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""WorkersKV：agent 私有状态标记的 KV 门面（与 sources/kv.py 同一命名空间）。

★与 sources/kv.py（CloudflareKV）的差别：
    本类   键统一带 `agent:` 前缀（agent 自己的台账状态等），fail-fast（出错抛异常），
           复用共享 CloudflareClient。
    kv.py  共享业务表（saved / rejected / ai:v3: 等，无前缀），fail-open（读失败返回
           默认值并留痕），带进程锁 + atomic_update。
两者指向同一个 KV 命名空间（KV_NAMESPACE_ID），只是键前缀与错误语义不同；
设计说明见 tools/adapters/__init__.py。
"""

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
