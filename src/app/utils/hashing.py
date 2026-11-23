# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""跨进程稳定的短哈希：同一组入参在任何进程、任何启动时间都得到同一个键。

幂等写（台账 upsert、索引上报队列）不能用 `abs(hash(...))`——CPython 的字符串哈希
带进程级随机种子，重启后同一个业务对象会算出不同的键，幂等变成「每次都新建」。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_key(*parts: Any) -> str:
    """把入参按 key 排序序列化后取 sha256 前 32 位。

    `default=str`：dict / set / 枚举值 / datetime 等非 JSON 原生类型不能让幂等键本身抛错。
    """
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
