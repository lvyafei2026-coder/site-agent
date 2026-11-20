# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""词条业务的数据通道 + 外部取数（按业务域划分，不是按存储资源划分）。

本包只放 adapters 层叶子；2026-11-20 分层归位后，编排/节点/业务服务已上移：
    graph/hotword_graph.py     后台循环图装配（fetch_one → analyze_one → 条件边回环）。
    graph/hotword_batch.py     批次驱动：构造 state → invoke 图 → summarize。
    nodes/hotword_nodes.py     循环节点：state / fetch_one / analyze_one / route / summarize。
    services/hotword_service.py   单词分析编排 _analyze_one（失败隔离到单词）。
    services/backfill_service.py  状态回填 backfill_states。

数据通道与业务读写（都落在共享 KV 上；KV 操作实现统一在 ../cloudflare/kv.py）：
    curation.py          词库整理域：候选池只读快照（candidates / meta / interests）
                         + 收藏 / 「不做」表写侧口径（与 console store.js 对齐）。
    hotwords.py          公共 API 门面：只聚合本层叶子符号，不承载逻辑、不 re-export 上层入口。
    hotword_contract.py  共享契约叶子：候选筛选规则 + trace 常量 + 成功阈值（无上层依赖）。
    hotword_fetch.py     取数层：SERP / Trends 读缓存 → 现抓 → 写回 + 全局节流。
    hotword_criteria.py  纯判据：提示词拼装、结论/红线/信号标签解析（无 IO）。
    hotword_llm.py       LLM 调用与空正文诊断。
    hotword_kv_io.py     KV 写账本（_WriteLog）+ 形状兜底读取。

依赖单向无环：graph → nodes → services → adapters（terms 叶子）→ cloudflare.kv。
上层入口统一由 src.app.graph 包导出；本包不反向依赖 graph / nodes / services
（见 test_architecture.py::test_adapters_are_leaves）。

外部取数：
    gsc.py / reddit.py / scrape_serp.py / scrape_trend.py
    source_errors.py  抓取层↔业务层的异常契约（SourceUnavailable：源级不可用 vs 单词降级）

重依赖（google-api-python-client）一律在模块内延迟导入，导入本包不需要凭据。
"""

from . import gsc, reddit

__all__ = ["gsc", "reddit"]
