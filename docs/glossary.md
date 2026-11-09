<!--
Copyright (c) 2026 lvyafei · https://toolara.dev
SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
-->

# 术语表

> 三类：站群 / 编排 / 数据。查不到的词就搜代码，术语在代码注释里第一次出现时都有解释。

## 一、站群与部署

| 术语 | 解释 |
|---|---|
| **子站（site）** | 一个关键词一个站，独立仓库 + 独立 Cloudflare Worker。清单在 `config/sites.json`，`name` 就是子域名。 |
| **资产目录（`asset_dir`）** | 静态文件所在目录，默认 `public`，必须与仓库 `wrangler.toml` 的 `assets.directory` 一致，否则渲染出来的页面不会被部署。 |
| **Worker** | Cloudflare 的边缘计算单元。本站群每个子站一个 Worker，托管静态资产。 |
| **入口代理（`tool-proxy`）** | 站群统一入口 Worker，按 Host 转发、注入埋点、生成 robots/sitemap。它往 D1 写 `events`，本站只读。 |
| **radar** | 选题雷达（`radar.<主域>`），扫 Google Suggest / Trends / SERP 并给候选词打分。本站只读 `/api/candidates`。 |
| **`site-console`** | 站群控制台（页面与统计）。本站不提供 UI，两者共享 D1 / KV。 |
| **check run** | GitHub 上一次 CI 的结果。Cloudflare 部署 Worker 时会产生 `Workers Builds: <服务名>`，**判断有没有上线先看它**。 |
| **`?v=<随机数>`** | 穿透 CDN 缓存的手法。`check_live_page` 用它确认线上真的是新版本。 |
| **slug** | URL 里的页面标识，如 `paypal-fee-for-freelancers`。渲染前强校验 `^[a-z0-9][a-z0-9-]{2,60}$`，防路径穿越。 |
| **canonical** | 页面规范 URL，告诉搜索引擎同一内容以哪个地址为准。 |
| **JSON-LD** | 页面里的结构化数据（`application/ld+json`）。模板生成 `Article` + `FAQPage` 两段。 |
| **sitemap.xml** | 站点地图。`render_files` 往已有的 sitemap 里插 `<url>`；文件不存在则跳过。 |

## 二、Agent 编排

| 术语 | 解释 |
|---|---|
| **一轮（run）** | 一次 `POST /run` 的完整执行，结果是一份报告 + 可选的台账写入。没有跨轮状态。 |
| **意图（intent）** | 五选一的分类结果，决定默认调哪些工具。枚举在 `config.INTENTS`。 |
| **抽取（extraction）** | 从请求文本里抽出 `ExtractionSchema` 的六个字段，供校验与工具参数使用。 |
| **校验（validation）** | 字段覆盖率与置信度打分；不通过就重试，重试耗尽走 error 分支。 |
| **重试（retry）** | 整段 `run_processing` 重跑，`retry_count` 上限由 `MAX_VALIDATION_RETRIES` 控制（默认 2）。 |
| **覆盖率（field_coverage）** | 六个契约字段里有值的比例。数的是**原始**抽取结果，不先过 Pydantic 补默认值。 |
| **综合分（confidence_score）** | `coverage * 0.6 + confidence * 0.4`，与覆盖率同时 >= 0.5 才算通过。 |
| **checkpointer** | 图状态持久化。`memory`（本地/测试）或 `sqlite`（生产，落 `/data`）。只存「跑到哪」，不是业务数据库。 |
| **`thread_id`** | 一次执行的会话标识，决定状态落哪个会话；不参与业务判断。 |
| **工具（tool）** | 25 个之一，`BaseTool` 子类，配 Pydantic `Input`，返回 `ToolOutput`。 |
| **`kind`** | 工具的副作用分类：`read`（只读）、`ledger`（可重复的台账/文件写）、`write`（不可逆发布）。 |
| **门面（ops）** | `RuntimeService`：6 个域 `Ops` 的多继承组合，工具通过它访问外部 IO。装配在 `tools/registry.py`。 |
| **域（domain）** | 一组相关工具的归口文件，如 `ledger_tools.py` 里的 `LedgerOps` + 9 个工具类。 |
| **`ToolOutput`** | 统一返回体 `{tool, ok, result, error}`。工具异常一律转成 `ok=false`，不抛出。 |

## 三、数据

| 术语 | 解释 |
|---|---|
| **D1** | Cloudflare 的托管 SQLite。本站读写埋点 `events` 与台账 `agent_*`。 |
| **KV** | Cloudflare 键值存储。本站只用 `agent:` 前缀，与 radar / console 共用一个命名空间。 |
| **台账（ledger）** | 业务状态的唯一真相源：`agent_pages` / `agent_reports` / `agent_backlinks` / `agent_events` / `agent_reflections` / `agent_tool_calls` / `agent_idempotency`。 |
| **白名单契约（`LedgerSpec`）** | 每张台账表允许写哪些列、唯一键是什么、冲突怎么处理。调用方多传的键会被丢掉。 |
| **upsert / ignore / lookup** | 三种写入策略：冲突即覆盖 / 冲突即丢弃（事件不重复改回 pending）/ 先查再决定更新或插入（`agent_backlinks`）。 |
| **幂等键（`event_key`）** | 跨进程稳定的哈希（`stable_key`），保证同一动作重复执行只留一条。 |
| **`events`** | 入口代理写入的埋点表：`ts`(ms) / `site` / `event` / `path` / `sid`。本站只读。 |
| **PV / UV** | 浏览量 / 独立访客数（`COUNT(DISTINCT sid)`）。 |
| **GSC** | Google Search Console，官方 API 提供展示、点击、CTR、平均排名。 |
| **收录（indexed）** | Google 已把页面放进索引。**收录 ≠ 有排名**。 |
| **长尾词** | 搜索量低但意图明确的词，站群流量几乎全靠它。 |

## 四、容易混淆

| 容易混淆 | 区别 |
|---|---|
| checkpointer vs D1 | 前者只记「图跑到哪」，后者是业务数据；不要拿 checkpointer 做业务查询。 |
| `read` vs `ledger` vs `write` | 只读 / 可重复写 / 不可逆发布；只有 `write` 需要请求显式声明。 |
| 上线 vs 收录 | 上线 = 页面 200；收录 = 已进索引，中间可能隔几天到几周。 |
| 展示 vs 访问 | 展示是在搜索结果里被看到，访问是点进来。展示高 + 访问低通常是标题问题。 |
| 站点 `name` vs `repo` | `name` 是子域名与工具入参，`repo` 是 GitHub 仓库名，缺省相同但不必然相同。 |
