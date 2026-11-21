# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""工具层：30 个工具 = 声明（RuntimeTool 子类）+ 实现（同文件的 <Domain>Ops）。

六个域，业务域与 adapters 的包一一对应（cloudflare 是厂商层，各域共用）：

    hotwords_tools.py      6 个  get_hotwords / next_candidate / read_examples /
                                 fetch_serp / fetch_trend / write_hotword_result
                                 → adapters/hotwords
    measurement_tools.py   2 个  get_click_stats / get_search_analytics
                                 → cloudflare.database + adapters/measurement
    promotion_tools.py     2 个  get_latest_plan / write_promo_slot
                                 → cloudflare.database
    distribution_tools.py  4 个  get_backlink_ledger / search_reddit /
                                 list_devto_articles / publish_devto_article
                                 → cloudflare.database + adapters/distribution
    publishing_tools.py    7 个  get_page_inventory / get_build_status / check_live_page /
                                 render_files / wait_build / queue_indexing / git_push
                                 → cloudflare.database + adapters/publishing
    ledger_tools.py        9 个  get_flag / get_reflections / get_pending_events /
                                 write_ledger / set_flag / emit_event / record_reflection /
                                 consume_event / send_notice
                                 → cloudflare.database + cloudflare.kv + adapters/publishing(email)

装配只在 registry.py：RuntimeService 多继承 6 个 Ops 成门面 → ToolRegistry.bind(ops)
把门面与 LangSmith tracer 注入每个工具 → RuntimeTool.run 校验 Input、调 ops.method、
统一返回 ToolOutput{tool, ok, result, error, retryable}。base.py 是纯叶子，不认识任何域。
工具类只声明 tool_name / method / kind / fixed / Input / docstring，不覆盖 run()：
业务分支一律写在 Ops 里，覆盖 run() 会绕过 tracer 与失败分类。

kind（真源 enums.ToolKind）：read 15 / ledger 11 / write 4。
    read    无副作用
    ledger  写台账 / KV / 本地文件，幂等可重复执行（含热词线的 fetch_serp /
            fetch_trend 缓存回写与 write_hotword_result 的 upsert）
    write   不可逆的外部动作：git_push / wait_build（空提交重推）/
            publish_devto_article / send_notice
生产没有按 kind 的闸门：默认集 INTENT_TOOLS 只含 read，write 只有请求显式传 tools 才跑。

唯一入口 = services/tool_service.py，两条线共用同一 registry：
    常规线 execute_for_intent(intent) 按意图分发工具集；
    热词线 run(name, **kwargs) 逐词按节点调补证据 / 后置动作工具——「哪几步、调哪个工具」
    就是 graph/hotword_graph.py 里的连线（步骤名 → 节点 → 工具）。
两条线都是**编排**，编排本身不进 registry；LLM 与纯判据也不是工具（横切能力）。
"""
