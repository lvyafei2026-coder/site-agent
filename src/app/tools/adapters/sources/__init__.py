# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词业务的数据通道 + 外部取数（按业务域划分，不是按存储资源划分）。

数据通道与业务读写（都落在共享 KV 上；KV 操作实现统一在 ../cloudflare/kv.py）：
    curation.py   词库整理域：候选池只读快照（candidates / meta / interests）
                  + 收藏 / 「不做」表写侧口径（与 console store.js 对齐）。
    hotwords.py   热词分析业务：SERP / 趋势抓取 + LLM 分析 + 结论回写 KV。

外部取数：
    gsc.py / reddit.py / scrape_serp.py / scrape_trend.py

重依赖（google-api-python-client）一律在模块内延迟导入，导入本包不需要凭据。
"""

from . import gsc, reddit

__all__ = ["gsc", "reddit"]
