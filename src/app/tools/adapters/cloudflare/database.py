# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""D1Database：关系台账端口的 Cloudflare 实现。"""

from __future__ import annotations

from typing import Any

from .client import CloudflareClient


class D1Database:
    def __init__(self, client: CloudflareClient):
        self._client = client

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        result = self._client.result(
            "POST", "d1", "database", self._client.database_id, "query",
            json={"sql": sql, "params": params or []}, timeout=30,
        )
        return result[0].get("results", []) if result else []

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        """写操作。约定：Agent 不执行 DELETE。"""
        if sql.strip().lower().startswith("delete"):
            raise PermissionError("Agent 不允许对 D1 执行 DELETE")
        self.query(sql, params)
