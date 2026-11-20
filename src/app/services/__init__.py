# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""业务实现层：一个节点一个同名实现类，跨节点共用的能力单独成文件。

节点（nodes/）只读写 state，业务一律在这里：

    nodes/normal_intent_node.py      → intent_service.IntentService
    nodes/normal_processing_node.py  → processing_service.ProcessingService
    nodes/normal_validation_node.py  → validation_service.ValidationService
    nodes/hotword_analyze_node.py    → hotword_service（逐词分析编排）
    nodes/hotword_fetch_node.py      → tool_service（取词 / 判例只是工具调用，无独立 service）

共用能力（被上面的实现类组合，不直接对节点暴露业务语义）：

    extraction_service  按 ExtractionSchema 抽字段（processing 用）
    metrics_service     review_metrics 的站点表现分析（processing 用）
    tool_service        两条线共用的工具入口：常规线按意图分发（processing 用），
                        热词线逐词按名调 next_candidate / fetch_serp / fetch_trend /
                        write_hotword_result（hotword_service 与 hotword_fetch_node 用）
    prompt_service      从 D1 取提示词并留日志（intent / extraction / metrics 用）

LLM 传输与解析降级不在本层：见 llm/completion.py（complete_json / complete_text）。
normal_output_node / normal_error_node / hotword_summary_node 是纯渲染 / 纯聚合，
无 IO 也无业务分支，不另立空壳 service（渲染契约在 models/workflow.py）。
"""
