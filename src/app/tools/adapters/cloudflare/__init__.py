# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Cloudflare 适配器：一个 client + 两个按能力命名的端口实现。

端口约定（测试用同名方法的内存对象替换，不依赖具体厂商）：
    database.query(sql, params) / execute(sql, params)
    keyvalue.get / put
"""

from .client import CloudflareClient
from .database import D1Database
from .keyvalue import WorkersKV

_client = CloudflareClient()
database = D1Database(_client)
keyvalue = WorkersKV(_client)

__all__ = ["CloudflareClient", "D1Database", "WorkersKV", "database", "keyvalue"]
