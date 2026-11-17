# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词业务的数据通道 + 外部取数（按业务域划分，不是按存储资源划分）。

数据通道与业务读写（都落在共享 KV 上）：
    kv.py         共享 KV（RADAR_KV）的门面：REST 客户端 + 键名契约 + atomic_update。
                  与 cloudflare/keyvalue.py（agent: 前缀状态标记、fail-fast）是同一
                  命名空间的两个门面，差别见 adapters/__init__.py。
    candidates.py 候选词快照读取（只读 candidates / meta / interests 表）。
    curation.py   收藏 / 「不做」表的写侧口径（与 console store.js 对齐）。
    hotwords.py   热词分析业务：SERP / 趋势抓取 + LLM 分析 + 结论回写 KV。

外部取数：
    gsc.py / reddit.py / scrape_serp.py / scrape_trend.py

重依赖（google-api-python-client）一律在模块内延迟导入，导入本包不需要凭据。
"""

from . import gsc, candidates, reddit

__all__ = ["gsc", "candidates", "reddit"]
