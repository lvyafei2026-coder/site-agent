# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""KV 形状兜底读取：期望数组 / 对象的键，形状不对按空处理并 warn（不抛）。

radar / console / agent 三方共写同一个 KV 命名空间，任何一方改了形状（候选数组变成对象、
摘要表写成字符串）都不能让读取方的整次调用失败，更不能抛异常打断逐词回环。这里只认
「有 get_json 的对象」，不 import 任何 KV 实现，因此不依赖 adapters 分层。

热词线的写入账本（带阶段名的成败记录）是业务口径，留在
src/app/tools/adapters/hotwords/writeback.py。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def kv_list(kv: Any, key: str) -> list:
    """读一个「应该是数组」的 KV 键；形状不对当空并 warn，不抛。"""
    val = kv.get_json(key, [])
    if isinstance(val, list):
        return val
    if val is not None and val != []:
        log.warning("KV 键 %s 期望数组，实际 %s，按空处理", key, type(val).__name__)
    return []


def kv_dict(kv: Any, key: str) -> dict:
    """读一个「应该是对象」的 KV 键；形状不对当空并 warn，不抛。"""
    val = kv.get_json(key, {})
    if isinstance(val, dict):
        return val
    if val is not None and val != {}:
        log.warning("KV 键 %s 期望对象，实际 %s，按空处理", key, type(val).__name__)
    return {}
