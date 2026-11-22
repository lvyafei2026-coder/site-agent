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
    nodes/hotword_serp_node.py       → execute_for_step(serp)
    nodes/hotword_trend_node.py      → execute_for_step(trend)
    nodes/hotword_write_node.py      → execute_for_step(write_result)
    nodes/hotword_llm_node.py        → 不经本层：提示词组装与判定契约在
                                        tools/adapters/hotwords/（criteria、llm_io）
    nodes/hotword_collect_node.py    → 无 service：纯落账
    nodes/hotword_summary_node.py    → 无 service：纯聚合与成功率

共用能力（被上面的实现类组合，或被热词节点直接调用）：

    extraction_service  按 ExtractionSchema 抽字段（processing 用）
    metrics_service     review_metrics 的站点表现分析（processing 用）
    tool_service        两条线共用的工具入口与名字表：常规线 execute_for_intent
                        （processing 用），热词线 execute_for_step（上表四个节点直接用）
    prompt_service      从 D1 取提示词并留日志（intent / extraction / metrics 用；
                        逐词提示词在 adapters 侧直接 src.app.prompts.load_prompt）

LLM 传输与解析降级不在本层：见 llm/completion.py（complete_json / complete_text）。
"""
