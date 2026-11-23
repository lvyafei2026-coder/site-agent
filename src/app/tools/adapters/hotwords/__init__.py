# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""hotwords 域适配器：词条业务的数据通道 + 外部取数（消费方 tools/hotwords_tools.py 与热词线）。

本包只放 adapters 层叶子；2026-11-20 分层归位后，编排/节点/业务服务已上移：
    graph/hotword_graph.py     循环图装配 + 运行入口 run_hotwords，含逐词子图与步骤白名单。
    graph/state.py             HotwordLoopState + initial_hotword_state（与常规线并列）。
    nodes/hotword_*_node.py    一节点一文件：取词 / 补证据 / LLM / 后置动作 / 落账 / 汇总
                               （回环与分支路由在 graph/edges.py）。

数据通道与业务读写（都落在共享 KV 上；KV 操作实现统一在 ../cloudflare/kv.py）：
    fetch.py        取数层：SERP / Trends 读缓存 → 现抓 → 写回 + 全局节流。
    wordlib.py      词库：候选池快照 / 取下一个未跟进的词 / 读判例，外加 saved /
                    rejected / aihints 三张表的写侧口径（与 console store.js 对齐）。
                    一条词的结果形状（那几个键名）不在本包——由取词节点
                    nodes/hotword_fetch_node.py 铺，落账节点收尾。
    writeback.py    KV 写入层：写入账本 WriteLog（每次写入的成败与阶段名）+
                    结论落库（正文 / 标签 / 状态，正文先写、失败即收手）。

外部取数：
    scrape_serp.py / scrape_trend.py   单个源怎么抓、怎么解析

判据不在本包（2026-11-23 上移：adapters 只做外部系统 IO 与表读写）：
    prompts/hotword_messages.py       提示词拼装 + 本线 max_tokens=2000 口径
    nodes/hotword_llm_node.py         结论 / 红线 / 信号标签解析
    nodes/hotword_serp_node.py        SERP 证据降维与作品硬证据

与业务无关的横切件已上移到 src/app/utils/，本包只引用不再持有：
    utils/llm_io.py         LLM 传输与空正文诊断
    utils/kv_shape.py       kv_list / kv_dict 形状兜底读取
    utils/source_errors.py  SourceUnavailable：源级不可用 vs 单条降级

依赖单向无环：graph → nodes → services → tools（工具契约 + registry）→ adapters
（本包叶子）→ cloudflare.kv。热词线与常规线走同一套工具契约：本包的 KV / 抓取 IO 由
tools/hotwords_tools.py 的 6 个工具暴露，逐词子图里的 fetch_one 与各步骤节点经 tool_service
调用，不再自打 tool span。LLM 传输（utils/llm_io）与提示词拼装（prompts/hotword_messages）
不是工具，节点按层直接复用；结论与证据的判定就写在用它的节点里——与常规线
「LLM 走 llm/completion、不经 registry」一致。
上层入口统一由 src.app.graph 包导出；本包不反向依赖 graph / nodes / services / tools 契约
（见 test_architecture.py::test_adapters_are_leaves）。

★没有 re-export 门面：调用方（含测试）直接 import 需要的叶子模块，
  这样「谁用了哪一层」在 import 行上就能看出来。
"""
