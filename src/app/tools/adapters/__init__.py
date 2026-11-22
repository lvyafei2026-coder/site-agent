# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""外部系统适配器：所有真实 IO 只在这里发生（tools 层不得自己 requests / git / 发信）。

★包划分 = 「厂商基础设施 + 业务域」，**不是按存储资源类型**（KV / D1 / git）分。
  业务域包用 tools/ 的域名，域有外部系统才建包，看消费方就知道该进哪个包：

  cloudflare/    厂商层：一个 CloudflareClient + 三个端口实现（D1 database、
                 agent 私有状态 keyvalue、SQL 契约 sql）。换厂商只动这个包。
                 ledger / promotion 两个域只用它（纯台账，无外部系统）。
  hotwords/      词条业务：候选快照与整理口径、SERP / Trends 取数、判据、LLM、KV 写账本。
                 消费方 tools/hotwords_tools.py + 热词线（graph / nodes 直接 import 叶子）。
  metrics/       运营效果：Search Console 的曝光 / 排名取数（站内点击聚在 cloudflare/database）。
                 消费方 tools/metrics_tools.py。
  distribution/  外链分发：Reddit 只读搜索 + Dev.to 列热门 / 发文。
                 消费方 tools/distribution_tools.py。
  publishing/    站点发布：GitHub 仓库、页面渲染、构建验活。
                 消费方 tools/publishing_tools.py。
  notification/  运营触达：Resend 发信（日报 / 告警 / 结果摘要）。
                 消费方 tools/notification_tools.py。发信和发布是两件事：git_push 可以
                 重推、页面可以重建，邮件发出去收不回，所以单独成包，不让 publishing
                 的幂等口径盖住它。

  包名跟 tools/ 的域同名，一个 <Domain>Ops 只 import cloudflare 与自己同名的包；
  跨包引用一出现，「改一个外部系统只动一个包」就失效（守卫测试拦的就是这个）。

关于 KV（只有一处实现）：
  cloudflare/kv.py 是全项目**唯一的 KV 操作模块**，里面有两个门面指向
  同一个 KV 命名空间（KV_NAMESPACE_ID）：
    CloudflareKV = 共享业务表（saved / rejected / ai:v3: 等），fail-open（读失败
      返回默认值并留痕），带进程锁 + atomic_update + 键名契约。
    WorkersKV = agent 私有状态标记（键带 `agent:` 前缀），fail-fast（出错抛异常）。
  curation / hotword_kv_io 是业务读写逻辑，都经由这两个门面落 KV。

子包按需 import，这里不做再导出——避免任一适配器被用到时把所有包全拉起来。
"""
