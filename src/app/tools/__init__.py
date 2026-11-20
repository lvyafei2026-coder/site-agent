# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""工具层：25 个工具 = 声明（RuntimeTool 子类）+ 实现（同文件的 <Domain>Ops）。

六个域，业务域与 adapters 的包一一对应（cloudflare 是厂商层，各域共用）：

    hotwords_tools.py      1 个  get_hotwords
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

kind（真源 enums.ToolKind）：read 13 / ledger 8 / write 4。
    read    无副作用
    ledger  写台账 / KV / 本地文件，幂等可重复执行
    write   不可逆的外部动作：git_push / wait_build（空提交重推）/
            publish_devto_article / send_notice
生产没有按 kind 的闸门：默认集 INTENT_TOOLS 只含 read，write 只有请求显式传 tools 才跑。

唯一入口 = services/tool_service.py（按意图分发）。热词分析是编排不是工具，
绕过 registry 直连 adapters/hotwords 叶子（见 graph/hotword_graph.py）。
"""
