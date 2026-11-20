# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""外部系统适配器：所有真实 IO 只在这里发生（tools 层不得自己 requests / git / 发信）。

★包划分 = 「厂商基础设施 + 业务域」，**不是按存储资源类型**（KV / D1 / git）分。
  业务域包与 tools/ 的域一一对应，看消费方就知道该进哪个包：

  cloudflare/    厂商层：一个 CloudflareClient + 三个端口实现（D1 database、
                 agent 私有状态 keyvalue、SQL 契约 sql）。换厂商只动这个包。
                 ledger / promotion 两个域只用它（纯台账，无外部系统）。
  hotwords/      词条业务：候选快照与整理口径、SERP / Trends 取数、判据、LLM、KV 写账本。
                 消费方 tools/hotwords_tools.py + 热词线（graph / nodes / hotword_service）。
  measurement/   效果度量：Search Console 取数（埋点聚合在 cloudflare/database）。
                 消费方 tools/measurement_tools.py。
  distribution/  外链分发：Reddit 只读搜索 + Dev.to 列热门 / 发文。
                 消费方 tools/distribution_tools.py。
  publishing/    站点发布：GitHub 仓库、页面渲染、构建验活 + Resend 运营通知。
                 消费方 tools/publishing_tools.py、tools/ledger_tools.py(send_notice)。

关于 KV（只有一处实现）：
  cloudflare/kv.py 是全项目**唯一的 KV 操作模块**，里面有两个门面指向
  同一个 KV 命名空间（KV_NAMESPACE_ID）：
    CloudflareKV = 共享业务表（saved / rejected / ai:v3: 等），fail-open（读失败
      返回默认值并留痕），带进程锁 + atomic_update + 键名契约。
    WorkersKV = agent 私有状态标记（键带 `agent:` 前缀），fail-fast（出错抛异常）。
  curation / hotword_kv_io 是业务读写逻辑，都经由这两个门面落 KV。

子包按需 import，这里不做再导出——避免任一适配器被用到时把五组全拉起来。
"""
