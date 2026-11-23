# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""业务实现层：常规线一个节点一个同名实现类，热词线的编排不在这里。

节点（nodes/）只读写 state，业务规则一律在本层：

    nodes/normal_intent_node.py      → intent_service.IntentService
    nodes/normal_processing_node.py  → processing_service.ProcessingService
    nodes/normal_validation_node.py  → validation_service.ValidationService
    nodes/normal_output_node.py      → 无 service：纯渲染（契约在 models/workflow.py）
    nodes/normal_error_node.py       → 无 service：纯兜底文案

热词线没有 service 类——逐词编排就是 graph/hotword_graph.py 按名单编译出来的
**逐词子图**，补证据 / LLM / 后置动作 / 落账各是一个节点，名单 → 节点的对应表
和白名单校验也在那张图里。本层只承担这些节点里的「一次工具调用」，且与常规线
同样是表驱动：节点传 HotwordStep 步骤名，步骤 → 工具的路由表是 tool_service.STEP_TOOLS。

    nodes/hotword_fetch_node.py      → execute_for_step(fetch / examples)
    nodes/hotword_serp_node.py       → execute_for_step(serp)；SERP 降维与作品硬证据写在节点里
    nodes/hotword_trend_node.py      → execute_for_step(trend)
    nodes/hotword_write_node.py      → execute_for_step(write_result)
    nodes/hotword_llm_node.py        → execute 之外只借 src/app/prompts/hotword_messages
                                       （build_messages、max_tokens）与 src/app/utils/llm_io
                                       （传输）；结论 / 红线 / 信号标签的解析写在节点里。
                                       与常规线一样「LLM 不经工具 registry」
    nodes/hotword_collect_node.py    → 无 service：纯落账
    nodes/hotword_summary_node.py    → 无 service：纯聚合与成功率

共用能力（被上面的实现类组合，或被热词节点直接调用）：

    extraction_service  按 ExtractionSchema 抽字段（processing 用）
    metrics_service     review_metrics 的站点表现分析（processing 用）
    tool_service        两条线共用的工具入口与名字表：常规线 execute_for_intent
                        （processing 用），热词线 execute_for_step（上表四个节点直接用）
    prompt_service      从 D1 取提示词并留日志（intent / extraction / metrics 用；
                        逐词提示词不走这里：本层没有热词编排，且 prompts 包不许反向
                        依赖 services，见 src/app/prompts/hotword_messages.py）

热词线在本层没有口径模块：一条词的输入怎么拼在 src/app/prompts/hotword_messages，
判定与解析在两个节点里，成败怎么落库在 tools/adapters/hotwords/writeback。

LLM 传输与解析降级不在本层：见 llm/completion.py（complete_json / complete_text）。
"""
