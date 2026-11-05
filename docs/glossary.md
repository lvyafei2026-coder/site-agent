<!--
Copyright (c) 2026 lvyafei · https://toolara.dev
SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
-->

# 术语表

> 按三个领域分组。查不到的词可以直接搜代码，或提 issue。

---

## 一、Cloudflare 与部署

| 术语 | 解释 |
|---|---|
| **Worker** | Cloudflare 的边缘计算单元。一段 JS 代码跑在全球节点上，无服务器。本项目 33 个子站各是一个 Worker。 |
| **service binding** | Worker 之间互相调用的内部通道。`tool-proxy` 靠它把请求转给子站，不经过公网。binding 缺失时直接 503 并打日志。 |
| **D1** | Cloudflare 的 SQLite 托管数据库。本项目用它存埋点 `events`、选题 `candidates`、Agent 台账 `agent_*`。 |
| **KV** | Cloudflare 的键值存储，全球缓存、最终一致。本项目用它存熔断标记与审批去重键。 |
| **R2** | Cloudflare 的对象存储（兼容 S3）。本项目用它存 `music` 站的封面图。 |
| **Workers AI** | Cloudflare 的边缘推理服务。**账户级配额 10,000 neurons/天，所有 Worker 共用**，本项目完全不使用（留给绘图站）。 |
| **check run** | GitHub 上 CI 一次运行的结果记录。Cloudflare 部署 Worker 时会产生名为 `Workers Builds: <服务名>` 的 check run。**判断有没有上线，先看它**。 |
| **`?v=<随机数>`** | 穿透 CDN 缓存的惯用手法。请求时带一个变化的 query，强制回源。本项目用它验证"线上页面真的是新版"。 |
| **canonical** | 页面的规范 URL（`<link rel="canonical">`）。用于告诉搜索引擎"同一内容以这个地址为准"。 |

---

## 二、Agent 编排（LangGraph）

| 术语 | 解释 |
|---|---|
| **StateGraph** | LangGraph 的核心抽象：一张有向图，节点是函数，边是流转。本项目用它表达 `sense → decide → execute → verify → learn`。 |
| **node（节点）** | 图上的一个处理步骤。本项目有 5 个，都是纯函数（只做编排，不做 IO）。 |
| **checkpointer** | 状态持久化器。每执行完一个节点就把图状态落盘，进程重启后能从断点继续。本项目用 SQLite 实现。 |
| **`thread_id`** | 一条执行线程的标识。本项目用业务主键（`gen-page:2026-11-05`）代替随机 UUID，因此重复触发会续跑同一条线程而不是新建。 |
| **`interrupt()`** | 在节点内部主动挂起执行，等人给输入。本项目用它实现"写操作等人批准"。 |
| **resume** | 恢复被挂起的执行。**注意：恢复时整个节点函数会从头重跑**，只是这次 `interrupt()` 直接返回人的输入——所以节点内的副作用必须幂等。 |
| **`Command(resume=...)`** | 触发 resume 的调用形式。 |
| **幂等（idempotent）** | 同一次操作执行多次，结果与执行一次相同。本项目的 `execute_action` 必须满足这条，否则 resume 会重复建页。 |
| **熔断（breaker）** | 连续失败 N 次后暂停该类型任务 M 分钟，防止持续烧钱或刷屏。状态存 KV，键为 `agent:pause:<scope>`。 |
| **fail-open** | 保护装置自身故障时放行，而不是阻断。本项目的熔断器读 KV 失败时视为"未熔断"。 |

---

## 三、业务与 SEO

| 术语 | 解释 |
|---|---|
| **tool-proxy** | 站群的统一入口 Worker。所有子域都绑定在它上面，由它分发、生成 robots/sitemap、注入埋点、处理 Google 登录。 |
| **radar** | 内部选题雷达（`radar.toolara.dev`）。扫 Google Suggest / Trends / X 热榜，算 SERP 竞争度与 30 天趋势，给候选词打分。**不对外**。 |
| **candidates** | radar 的候选词表（D1）。每条记录含关键词、搜索量、竞争度、趋势、`verdict`。 |
| **verdict** | radar 对候选词的结论：`做` / `不做` / `parked`（暂时搁置）。本 Agent 只取 `做` 的。 |
| **DEAD_RULES** | 判死规则：真人姓名、他人作品名、纯资讯类词一律不做。radar 有一份，本 Agent 的 `DEAD_PATTERNS` 是第二道闸。 |
| **`events`** | tool-proxy 的埋点表（D1）。记录页面访问与点击事件，是本项目"数据解读"的原料。 |
| **PV / UV** | 页面浏览量 / 独立访客数。 |
| **跳出率（bounce）** | 只访问一页就离开的比例。工具站天然偏高，看趋势不看绝对值。 |
| **GSC** | Google Search Console。官方后台，提供展示次数、点击次数、CTR、平均排名。**有官方 API**，本项目不抓网页。 |
| **展示 / 点击 / CTR** | 搜索结果里出现次数 / 被点击数 / 点击率。`CTR 低 + 展示高` 通常意味着标题或描述需要重写。 |
| **Indexing API** | GSC 的 URL 提交接口，配额 **200 次/天**。提交不等于收录，只是"通知 Google 来看看"。 |
| **sitemap.xml** | 站点地图。本项目在新增页面后往目标子站的 sitemap 里插一条 `<url>`。 |
| **收录（indexed）** | Google 已把页面存进索引、可以出现在搜索结果里。**收录 ≠ 有排名**。 |
| **长尾词** | 搜索量低但意图明确的词（如 `paypal fee calculator for freelancers`）。33 个子站的流量几乎全靠长尾。 |
| **slug** | URL 里的页面标识，如 `paypal-fee-for-freelancers`。本项目强校验 `^[a-z0-9][a-z0-9-]{2,60}$`，防路径穿越。 |
| **JSON-LD** | 页面里的结构化数据脚本（`application/ld+json`）。本项目生成 `Article` + `FAQPage` 两段。 |
| **OG 图** | 社交分享时显示的预览图。站群统一 1200×630，由 tool-proxy 动态生成。 |
| **engaged views** | YouTube 的"有效观看"指标（看过一定时长才算）。比播放数更能反映内容质量。 |
| **养号** | 在新平台上用正常互动积累账号权重，避免新号直接发链接被判垃圾。本项目的 Reddit / HN 账号都处于这个阶段。 |

---

## 四、几个容易混淆的词

| 容易混淆 | 区别 |
|---|---|
| `thread_id` vs `Action.key` | 前者标识"一次任务"，后者标识"一个动作"。一个线程里可能有多个动作。 |
| checkpointer vs D1 | 前者只存"图跑到哪一步"，后者是业务数据的唯一真相源。**不要拿 checkpointer 做业务查询。** |
| 熔断 vs 报错 | 报错是单次失败，熔断是"连续失败后主动暂停"。有熔断不代表代码有 bug，可能只是外部服务临时挂了。 |
| 上线 vs 收录 | 上线 = 页面能访问（200）；收录 = Google 已索引。中间可能隔几天到几周。 |
| 展示 vs 访问 | 展示 = 在搜索结果里被看到；访问 = 用户真的点进来。`展示高 + 访问低` 是标题问题，不是内容问题。 |
