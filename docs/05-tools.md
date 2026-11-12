# 05 · 工具与配置速查

> 26 个工具一张表；意图到工具集的映射；站点清单字段；新增工具的检查清单。
> 契约与实现细节见 `03-tech-design.md §5`。

## 1. 全量工具（26）

`kind` 说明：`read` 只读；`ledger` 写台账 / KV / 本地文件，可重复执行；
`write` 不可逆的外部发布。

### 1.1 selection（2）

| name | kind | method | 参数 | 用途 |
|---|---|---|---|---|
| `get_hotwords` | read | `hotwords` | `status=doing`、`limit=40` | 读 radar 候选词，返回 `items` + `meta`（`updatedMs` / `geosOk` / `raw`）。**判断有没有新数据要看 meta** |
| `analyze_hotwords` | **write** | `analyze_hotwords` | `limit=20` | 热词分析（2026-11-12 从 site-console「开始分析」迁入）：经 console `/api/hotwords` 取 20 条「未跟进」词，逐条调 radar `/api/serp` + `/api/trend` 补数据、外部 LLM 出 8 小节站点方案，再写回正文 / 信号 / 「不做」或「待开发」。★`analyze_hotwords` 意图的默认集就是它本身，分类到该意图即执行；也可显式传 `tools` 触发 |

### 1.2 measurement（2）

| name | kind | method | 参数 | 用途 |
|---|---|---|---|---|
| `get_click_stats` | read | `click_stats` | `days=7` | 按站点/天聚合埋点：`daily`（PV、UV）+ `top_pages`（UV 用 `COUNT(DISTINCT sid)`） |
| `get_search_analytics` | read | `search_analytics` | `days=7` | GSC 点击 / 展示 / 排名，固定 `dimensions=("page",)`；无凭据返回 `[]` |

### 1.3 distribution（4）

| name | kind | method | 参数 | 用途 |
|---|---|---|---|---|
| `get_backlink_ledger` | read | `backlink_ledger` | `limit=200` | 外链台账里未失效的记录（去重用） |
| `search_reddit` | read | `reddit_search` | `query`、`limit=8` | 只读搜索 Reddit 讨论，找外链机会 |
| `list_devto_articles` | read | `devto_articles` | `tags=["ai","webdev","productivity"]` | 按标签列 Dev.to 热门文章 |
| `publish_devto_article` | **write** | `publish_devto` | `payload` | 调官方 API 发文；无 `DEVTO_API_KEY` 返回 `blocked` |

### 1.4 ledger（9）

| name | kind | method | 参数 | 用途 |
|---|---|---|---|---|
| `get_flag` | read | `flag` | `key` | 读 `agent:` 前缀的 KV 状态标记 |
| `get_reflections` | read | `reflections` | `limit=8` | 最近的复盘 / 反思记录 |
| `get_pending_events` | read | `events` | `limit=50` | `agent_events` 里 `status=pending` 的事件 |
| `write_ledger` | ledger | `ledger_upsert` | `table`、`row` | 按 `LEDGER_TABLES` 白名单 UPSERT 一行 |
| `set_flag` | ledger | `flag_set` | `key`、`value`、`ttl_sec?` | 写 KV 状态标记，可带过期 |
| `emit_event` | ledger | `event_emit` | `kind`、`payload`、`key` | 往事件表写一条（`key` 是幂等键，重复写被忽略） |
| `record_reflection` | ledger | `reflection_add` | `slot`、`reason`、`detail`、`action_key?` | 记一次失败原因 / 效果反思 |
| `consume_event` | ledger | `event_consume` | `event_key`、`thread_id?` | 把事件标记为已消费 |
| `send_notice` | ledger | `send_notice` | `subject`、`html` | Resend 发通知邮件；缺凭据返回 `sent=false` |

### 1.5 promotion（2）

| name | kind | method | 参数 | 用途 |
|---|---|---|---|---|
| `get_latest_plan` | read | `latest_plan` | — | 最近一份推广排期（`agent_reports.kind='plan'`） |
| `write_promo_slot` | ledger | `run()` 自带分支 | `item` | 同日同渠道去重后追加到排期；`status=scheduled` 或 `skipped` |

### 1.6 publishing（7）

| name | kind | method | 参数 | 用途 |
|---|---|---|---|---|
| `get_page_inventory` | read | `page_inventory` | `mode=all\|recent\|new\|review`、`limit=200` | 页面台账；`new` 近 30 天、`review` 上线满 7 天 |
| `get_build_status` | read | `build_status` | `repo`、`sha` | GitHub check runs |
| `check_live_page` | read | `live_page` | `site_name`、`slug`、`expect?` | 带随机参数验活：200 且含预期标题 |
| `render_files` | ledger | `render_files` | `site_name`、`page` | 渲染 HTML 写进本地副本 + 更新 sitemap + `git add` |
| `wait_build` | ledger | `wait_build` | `repo`、`sha`、`work?`、`branch=main` | 轮询构建（360s），失败且给了 `work` 时空提交重推一次 |
| `queue_indexing` | ledger | `queue_indexing` | `url` | 往事件表记一条待收录 |
| `git_push` | **write** | `git_push` | `site_name`、`page`、`work` | commit + push，触发 CF 构建；无变更则 `skipped` |

## 2. 意图 → 默认工具

| 意图 | 默认集 |
|---|---|
| `select_pages` | `get_hotwords` + `get_page_inventory` + `get_reflections` |
| `plan_promo` | `get_latest_plan` + `get_page_inventory` + `get_pending_events` |
| `find_backlinks` | `get_backlink_ledger` + `search_reddit` + `list_devto_articles` |
| `review_metrics` | `get_click_stats` + `get_search_analytics` + `get_page_inventory` |
| **`analyze_hotwords`** | **`analyze_hotwords`**（★唯一的默认集里带 write 工具的意图：这个意图的业务本身就是「分析并改状态」，LLM 分类到它即会执行，无需显式传 `tools`） |
| `general` | `get_pending_events` + `get_reflections` |

默认集一般只含 read 工具，唯一例外是 `analyze_hotwords`：它的产出就是状态变更。
`TOOL_ARGS`（`services/tool_service.py`）给几个工具预置参数：热词取 20 条、分析批次 20 条、页面台账取 `mode=new` 10 条、Reddit 搜索词取请求文本前 80 字等。

请求里传 `tools` 会完全覆盖默认集，包含写工具也一样——这是触发不可逆动作的
唯一开关：

```json
{"input_text": "把 crcl 的新页推上线", "tools": ["render_files", "git_push"]}
```

## 3. 站点清单 `config/sites.json`

```json
{
  "sites": [
    {"name": "crcl", "repo": "cockcroft-gault-calculator",
     "brand": "Cockcroft-Gault Calculator", "lang": "en",
     "branch": "main", "daily_limit": 5, "asset_dir": "public"}
  ]
}
```

| 字段 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `name` | ✅ | — | 站点标识 = 子域名，写工具用它定位站点 |
| `repo` | 否 | 同 `name` | GitHub 仓库名（`<GH_ORG>/repo`） |
| `subdomain` | 否 | 同 `name` | 子域名；空串表示主域本身 |
| `brand` | 否 | `""` | 页面品牌名 |
| `lang` | 否 | `en` | `<html lang>` |
| `branch` | 否 | `main` | push 目标分支 |
| `daily_limit` | 否 | `5` | 单站每日建页上限（调用方遵守） |
| `asset_dir` | 否 | `public` | 静态资产目录，必须与仓库 `wrangler.toml` 的 `assets.directory` 一致 |

站点重名会在启动时抛 `ValueError`，不会静默取最后一个。
`SITES_FILE` 可指向另一份清单（多环境）。

## 4. 新增能力检查清单

**加一个工具**

1. 在对应域 `*_tools.py` 的 `<Domain>Ops` 里写实现方法；
2. 同文件加工具类：docstring（= 描述）+ `tool_name` / `method`（+ 非只读时 `kind`）+ 嵌套 `Input`；
3. `tools/registry.py` 的 `TOOL_CLASSES` 加一行；
4. 想进默认编排就改 `services/tool_service.py` 的 `INTENT_TOOLS`（可选）；
5. `pytest -q`：`test_every_tool_method_is_implemented_and_used` 会校验
   「工具声明的 method 都有实现，且门面没有多余方法」。

**加一个域**

1. 新建 `<domain>_tools.py`（`<Domain>Ops` + 工具类）；
2. `registry.py` 里 import 该 Ops，并加进 `RuntimeService` 的基类列表；
3. 工具类登记到 `TOOL_CLASSES`。

**加一个站点**

只改 `config/sites.json`。前提：仓库是资产目录布局、页面能套
`templates/site-page.html`、`GITHUB_TOKEN` 对该仓库有 Contents 写权限。

**加一个意图**

1. `config.INTENTS` 加枚举值；
2. `INTENT_TOOLS` 加映射；
3. 需要的话加 `prompts/versions/intent/vN.txt`，请求里传 `prompt_versions` 使用。
