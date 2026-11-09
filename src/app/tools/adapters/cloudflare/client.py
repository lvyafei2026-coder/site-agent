# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Cloudflare REST 客户端：账号、令牌、URL 拼装、错误解包只在这里出现一次。

所有凭据都是 lazy 读取，因此无凭据的环境也能安全 import 本模块。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from src.app.config import env

API = "https://api.cloudflare.com/client/v4"


class CloudflareClient:
    def __init__(self, *, account_id: str = "", api_token: str = "",
                 database_id: str = "", namespace_id: str = ""):
        self._account_id = account_id
        self._api_token = api_token
        self._database_id = database_id
        self._namespace_id = namespace_id

    @property
    def account_id(self) -> str:
        return self._account_id or env("CF_ACCOUNT_ID", required=True)

    @property
    def api_token(self) -> str:
        return self._api_token or env("CF_API_TOKEN", required=True)

    @property
    def database_id(self) -> str:
        return self._database_id or env("D1_DATABASE_ID", required=True)

    @property
    def namespace_id(self) -> str:
        return self._namespace_id or env("KV_NAMESPACE_ID", required=True)

    def url(self, *segments: str) -> str:
        path = "/".join(quote(str(s), safe=":") for s in segments)
        return f"{API}/accounts/{self.account_id}/{path}"

    def request(self, method: str, *segments: str, params=None, json=None,
                data=None, timeout: int = 30) -> requests.Response:
        return requests.request(
            method, self.url(*segments),
            headers={"Authorization": f"Bearer {self.api_token}"},
            params=params, json=json, data=data, timeout=timeout,
        )

    def result(self, method: str, *segments: str, **kw: Any) -> Any:
        """解包 {success, result}  envelope；业务错误统一收敛为 RuntimeError。"""
        resp = self.request(method, *segments, **kw)
        body = resp.json()
        if not body.get("success"):
            raise RuntimeError(f"Cloudflare {method} {'/'.join(segments)} 失败: {body.get('errors')}")
        return body.get("result") or []
