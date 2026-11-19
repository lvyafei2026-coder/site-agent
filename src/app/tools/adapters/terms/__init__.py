# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""词条业务的数据通道 + 外部取数（按业务域划分，不是按存储资源划分）。

数据通道与业务读写（都落在共享 KV 上；KV 操作实现统一在 ../cloudflare/kv.py）：
    curation.py          词库整理域：候选池只读快照（candidates / meta / interests）
                         + 收藏 / 「不做」表写侧口径（与 console store.js 对齐）。
    hotwords.py          热词分析公共 API 门面：聚合对外契约 + 稳定测试入口，不承载逻辑。
    hotword_contract.py  共享契约叶子：候选筛选规则 + trace 常量 + 成功阈值（无上层依赖）。
    hotword_fetch.py     取数层：SERP / Trends 读缓存 → 现抓 → 写回 + 全局节流。
    hotword_worker.py    单词层：补数据 → LLM → 结论判定 → 回写状态（失败隔离到单词）。
    hotword_batch.py     批次入口：批次 trace 外壳 + 对外契约，invoke 后台循环图。
    hotword_loop.py      逐词循环图的节点逻辑（state / fetch_one / analyze_one / route /
                         summarize）；图结构装配在 graph/hotword_graph.py，与本模块单向依赖。
    hotword_criteria.py  纯判据：提示词拼装、结论/红线/信号标签解析（无 IO）。
    hotword_llm.py       LLM 调用与空正文诊断。
    hotword_kv_io.py     KV 写账本（_WriteLog）+ 形状兜底读取。
    hotword_backfill.py  状态回填 backfill_states。

依赖单向无环：batch → hotword_graph → loop → worker → fetch / criteria / llm / kv_io / contract。
后台图装配下沉到 graph/hotword_graph.py 叶子模块（只依赖 loop），batch 顶层静态取图，无惰性边界。

外部取数：
    gsc.py / reddit.py / scrape_serp.py / scrape_trend.py
    source_errors.py  抓取层↔业务层的异常契约（SourceUnavailable：源级不可用 vs 单词降级）

重依赖（google-api-python-client）一律在模块内延迟导入，导入本包不需要凭据。
"""

from . import gsc, reddit

__all__ = ["gsc", "reddit"]
