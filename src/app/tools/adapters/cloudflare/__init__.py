# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Cloudflare 适配器：一个 client + KV 唯一模块 + D1 端口。

端口约定（测试用同名方法的内存对象替换，不依赖具体厂商）：
    database.query(sql, params) / execute(sql, params)
    keyvalue.get / put

★KV 的全部操作都在 kv.py 一个文件里：CloudflareKV（共享业务表、fail-open）
+ WorkersKV（agent: 前缀、fail-fast）+ atomic_update + table_lock + 键名契约。
"""

from .client import CloudflareClient
from .database import D1Database
from .kv import (
    CloudflareKV,
    WorkersKV,
    atomic_update,
    get_kv,
    table_lock,
)

_client = CloudflareClient()
database = D1Database(_client)
keyvalue = WorkersKV(CloudflareKV.from_env())

__all__ = [
    "CloudflareClient", "CloudflareKV", "D1Database", "WorkersKV",
    "atomic_update", "get_kv", "table_lock", "database", "keyvalue",
]
