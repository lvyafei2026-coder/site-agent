# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""词条业务的数据通道 + 外部取数（按业务域划分，不是按存储资源划分）。

数据通道与业务读写（都落在共享 KV 上；KV 操作实现统一在 ../cloudflare/kv.py）：
    curation.py          词库整理域：候选池只读快照（candidates / meta / interests）
                         + 收藏 / 「不做」表写侧口径（与 console store.js 对齐）。
    hotwords.py          热词分析门面：只做再导出，保持 hotwords.<name> 引用不变。
    hotword_fetch.py     取数层：SERP / Trends 读缓存 → 现抓 → 写回 + 全局节流。
    hotword_worker.py    单词层：补数据 → LLM → 结论判定 → 回写状态（失败隔离到单词）。
    hotword_batch.py     批次层：候选筛选 → 并发逐词 → 汇总 + 批次 trace 根。
    hotword_criteria.py  纯判据：提示词拼装、结论/红线/信号标签解析（无 IO）。
    hotword_llm.py       LLM 调用与空正文诊断。
    hotword_kv_io.py     KV 写账本（_WriteLog）+ 形状兜底读取。
    hotword_backfill.py  状态回填 backfill_states。

依赖单向：batch → worker → fetch → kv_io；门面只再导出，不承载逻辑。

外部取数：
    gsc.py / reddit.py / scrape_serp.py / scrape_trend.py
    source_errors.py  抓取层↔业务层的异常契约（SourceUnavailable：源级不可用 vs 单词降级）

重依赖（google-api-python-client）一律在模块内延迟导入，导入本包不需要凭据。
"""

from . import gsc, reddit

__all__ = ["gsc", "reddit"]
