# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""工具层：30 个工具 = 声明（RuntimeTool 子类）+ 实现（同文件的 <Domain>Ops）。

切分维度是**业务对象**，不是副作用等级——后者由 kind（read / ledger / write）单独表达，
同一个域里可以混合出现（publishing 既有只读的 get_build_status，也有不可逆的 git_push）。
七个域，有外部系统的域才有同名的 adapters 包（cloudflare 是厂商层，各域共用）：

    hotwords_tools.py      6 个  get_hotwords / next_candidate / read_examples /
                                 fetch_serp / fetch_trend / write_hotword_result
                                 → adapters/hotwords
    metrics_tools.py       2 个  get_click_stats / get_search_analytics
                                 （线上站点的实际运营效果：站内点击 + 站外曝光排名）
                                 → cloudflare.database + adapters/metrics
    promotion_tools.py     2 个  get_latest_plan / write_promo_slot
                                 → cloudflare.database（纯台账，无外部系统）
    distribution_tools.py  4 个  get_backlink_ledger / search_reddit /
                                 list_devto_articles / publish_devto_article
                                 → cloudflare.database + adapters/distribution
    publishing_tools.py    7 个  get_page_inventory / get_build_status / check_live_page /
                                 render_files / wait_build / queue_indexing / git_push
                                 → cloudflare.database + adapters/publishing
    ledger_tools.py        8 个  get_flag / get_reflections / get_pending_events /
                                 write_ledger / set_flag / emit_event / record_reflection /
                                 consume_event
                                 → cloudflare.database + cloudflare.kv
    notification_tools.py  1 个  send_notice
                                 → adapters/notification

一个域文件只允许 import cloudflare 与自己同名的 adapters 包（守卫见
test_architecture.test_tools_import_only_own_adapters）：域之间的横向依赖一旦回来，
「改一个外部系统只动一个包」就不再成立。发信从 ledger 挪出来就是这个规矩的产物——
台账写幂等可重放，邮件发出去收不回，两者放一个域会让人以为 ledger 可随便重跑。

域名与调用方同词：metrics 对齐 Intent.REVIEW_METRICS 与 services/metrics_service.py，
它就是流量复盘那条线取数的地方。

装配只在 registry.py：RuntimeService 多继承 7 个 Ops 成门面 → ToolRegistry.bind(ops)
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

唯一入口 = services/tool_service.py，两条线共用同一 registry，也共用同一套「表驱动」：
    常规线 execute_for_intent(intent)  意图 → 工具集（INTENT_TOOLS + TOOL_ARGS）；
    热词线 execute_for_step(step, ...)  逐词步骤 → 该步的工具（STEP_TOOLS），参数由节点给。
「本轮走哪几步」是 graph/hotword_graph.py 里的连线，「每一步调哪个工具」是 tool_service
里的路由表——节点与图都不写 registry 的工具名。
两条线都是**编排**，编排本身不进 registry；LLM 与纯判据也不是工具（横切能力）。
"""
