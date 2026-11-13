# 03 · 技术设计

> 实现细节：状态契约、图与节点、服务层、工具层、适配器、数据契约、可观测性。
> 只描述**现在是什么**，不含演进过程。

## 1. 状态契约 `WorkflowState`

`graph/state.py` 里的 `TypedDict(total=False)`，字段按用途分三组：

| 组 | 字段 | 类型 | 谁写 | 谁读 |
|---|---|---|---|---|
| 输入 | `input_text` | `str` | `/run` / CLI | intent、processing |
| 输入 | `prompt_versions` | `dict[str,str]` | `/run`（缺省用 `settings.default_prompt_versions`） | intent / processing 各取自己那份；`hotwords` 由 tool_service 透传给逐词分析 |
| 输入 | `requested_tools` | `list[str] \| None` | `/run` | tool_service（`None` = 按意图默认集） |
| 输入/中间 | `intent` | `str` | 调用方可强制指定；否则 intent 节点产出 | processing、输出报告 |
| 中间 | `confidence` | `float` | intent 节点 | validation、报告 |
| 中间 | `extracted_data` | `dict` | processing 节点 | validation |
| 中间 | `tool_results` | `dict[str, dict]` | processing 节点 | validation、报告 |
| 中间 | `validation` | `dict` | validation 节点 | 路由、output、error |
| 中间 | `retry_count` | `int` | processing 每次 `+1` | 路由 |
| 中间 | `messages` | `list[str]` | 各节点追加 | 仅排查用 |
| 输出 | `output` | `str` | output / error | `/run` 返回体 |
| 输出 | `error` | `str` | error | `/run` 返回体 |

入口状态由 `initial_state()` 统一构造（CLI 与 `/run` 共用），默认值只在那一处：

```python
{"input_text": ..., "retry_count": 0, "messages": [], "tool_results": {},
 "prompt_versions": {...}, "requested_tools": tools}
```

## 2. 图与路由

`graph/builder.py`：

```python
START → classify_intent → run_processing → validate_result
                                   ↑              │
                                   └── retry ─────┤
                                                  ├── complete → build_output → END
                                                  └── error    → handle_error → END
```

| 组件 | 内容 |
|---|---|
| `constants.Names` | `classify_intent` / `run_processing` / `validate_result` / `build_output` / `handle_error` |
| `constants.Tags` | `retry` / `complete` / `error` |
| `edges.route_after_validation` | `is_valid` → complete；`retry_count < MAX_VALIDATION_RETRIES` → retry；否则 error |
| `builder.get_graph()` | `@lru_cache(1)`：checkpointer 持连接（SQLite 尤甚），不能每请求重建 |

节点名必须带动词前缀：LangGraph 0.3.x 不允许节点名与 state 字段同名，
否则 `add_node` 抛 `'intent' is already being used as a state key`。

## 3. 五个节点

| 节点 | 读 | 写 | 失败语义 |
|---|---|---|---|
| `intent_node` | `input_text`、`intent`（可能已强制） | `intent`、`confidence`、`messages` | LLM 失败回退 `general` / `0.55`；强制指定时 `confidence=1.0` 且不调 LLM |
| `processing_node` | `input_text`、`intent`、`prompt_versions`、`requested_tools` | `extracted_data`、`tool_results`、`retry_count`、`messages` | 抽取失败用正则兜底；单个工具失败只记 `ok=false` |
| `validation_node` | `extracted_data`、`confidence`、`tool_results` | `validation`、`messages` | 全部工具失败时强制 `is_valid=false` 并写 `reason` |
| `output_node` | 整个 state | `output` | — |
| `error_node` | `validation`、`error` | `output`、`error` | 输出 `ERROR: <原因>` |

节点只做编排，不直接碰外部系统——所有 IO 都在 tools/adapters 里。

## 4. 服务层

### 4.1 `llm_service`：两次调用 + 确定性回退

| 方法 | 用途 | 无 `LLM_API_KEY` 或异常时 |
|---|---|---|
| `classify_intent(template, text)` | 归入 `INTENTS` 之一 | 返回 `{"intent": "general", "confidence": 0.55}` |
| `extract_fields(template, text, intent)` | 抽 `ExtractionSchema` 六个字段 | 关键词用正则 `[A-Za-z][A-Za-z0-9 -]{2,30}` 截 12 个，`notes` 取原文前 200 字 |

- 一次调用：`chat.completions.create(..., response_format={"type":"json_object"}, temperature=0)`；
  解析失败或异常都返回 `None`，由上层兜底，绝不抛给图。
- 非法意图（不在 `config.INTENTS` 里）统一回退 `general`。
- 每次调用给 `metrics` 打点：`llm.classify_intent.calls` / `llm.extract_fields.calls`。
- 客户端在 `llm/provider.py`：有 `LLM_BASE_URL` 就传 `base_url`，因此任何 OpenAI
  兼容服务都能接。

### 4.2 `prompt_service`：版本可指定、缺失回退

- 根目录 `src/app/prompts/versions/<task>/vN.txt`，**所有发给 LLM 的文本唯一存放处**：
  `intent`（v1/v2）、`intent_user`、`extraction`、`extraction_user`、
  `hotwords`（逐词 8 小节 system，原 `AI_SYSTEM`）、`hotwords_user`（逐词入参模板）。
  `*_user` 是同一任务的 human 模板，与 system 分文件存放。
  ★2026-11-13 起业务代码里不允许再出现整段提示词字面量（`test_prompts.py` 静态守着）。
- 解析顺序：请求 `prompt_versions[task]` → `settings.default_prompt_versions[task]` → `v1`；
  文件不存在时回退 `v1.txt`。默认版本清单在 `config.py`，与目录一一对应。
- 工具层（adapters）不能反向依赖 `services`，所以热词逐词分析走 `prompts/__init__.py`
  的纯加载函数 `_prompt_pair()`（带进程内缓存，一批 20 词只读一次磁盘）；
  `/run` 的 `prompt_versions.hotwords` 经 `processing → tool_service → AnalyzeHotwordsTool
  → SelectionOps → run_batch` 一路透传，批次 trace 的 inputs 里也记 `promptVersion`。
- 每次加载打日志 `prompt_loaded`（task + version），排查「这轮用的是哪版」很快。

### 4.3 `tool_service`：意图 → 工具集

| 意图 | 默认工具 |
|---|---|
| `select_pages` | `get_hotwords`、`get_page_inventory`、`get_reflections` |
| `plan_promo` | `get_latest_plan`、`get_page_inventory`、`get_pending_events` |
| `find_backlinks` | `get_backlink_ledger`、`search_reddit`、`list_devto_articles` |
| `review_metrics` | `get_click_stats`、`get_search_analytics`、`get_page_inventory` |
| `general` | `get_pending_events`、`get_reflections` |

- 请求传 `tools` 时**完全覆盖**默认集；这是触发写工具的唯一开关。
- `TOOL_ARGS` 把请求文本与抽取结果映射成工具参数（如把 `text[:80]` 当搜索词）。
- 未注册的工具名不抛异常，返回 `{"ok": false, "error": "unknown tool"}`。

### 4.4 `evaluation_service`：两个公式

```
field_coverage   = 6 个契约字段中有值的比例        （原始抽取结果直接数，不先过 Pydantic）
confidence_score = field_coverage * 0.6 + confidence * 0.4
is_valid         = field_coverage >= 0.5 且 confidence_score >= 0.5
```

- 覆盖率必须数**原始**抽取结果：先过 `ExtractionSchema` 会补默认值，
  把地板抬到 2/6，测不出 LLM 到底给了几个字段。
- 运营语义补一条（在 `validation_node`）：本轮有工具且全部 `ok=false` 时，
  强制 `is_valid=false`，`reason` 列出失败工具名。

## 5. 工具层

### 5.1 契约

| 类 | 作用 |
|---|---|
| `BaseTool(ABC)` | `name` / `description` + `run()` 的抽象契约（dataclass） |
| `RuntimeTool(BaseTool)` | 子类只声明 `tool_name` / `method` / `Input`；`run()` 把参数转给 `self.ops.<method>` |
| `ToolOutput(BaseModel)` | 统一返回体 `{tool, ok, result, error}` |

`kind` 三档，缺省 `read`，非只读必须显式写：

| kind | 数量 | 含义 |
|---|---|---|
| `read` | 13 | 只读外部数据，无副作用 |
| `ledger` | 10 | 写台账 / KV / 本地文件，可重复执行 |
| `write` | 2 | 不可逆的外部发布（`git_push`、`publish_devto_article`） |

### 5.2 一个工具类的固定写法

```python
class GetHotwordsTool(RuntimeTool):
    """读取选题雷达候选关键词，并返回 meta（…）。"""   # ← description 就是这行 docstring

    tool_name = "get_hotwords"
    method = "hotwords"          # 转调 self.ops.hotwords(...)
    # kind 缺省 read；ledger / write 才写

    class Input(BaseModel):
        limit: int = Field(40, description="最多返回几条")
```

- `description` 缺省取类 docstring（`tool_desc` 只在需要覆盖时写）；
  `test_tools.py` 断言「description == 类 docstring」，漏写 docstring 直接红。
- `fixed` 补进调用的固定参数，只有 `get_search_analytics` 用（`dimensions=("page",)`）。
- `run()` 异常一律收成 `ToolOutput(ok=false, error=前 300 字)`，单工具失败不拖垮整轮。

### 5.3 六个域与装配

| 文件 | Ops 实现 | 工具数 |
|---|---|---|
| `selection_tools.py` | `SelectionOps`（radar） | 1 |
| `measurement_tools.py` | `MeasurementOps`（D1 埋点、GSC） | 2 |
| `distribution_tools.py` | `DistributionOps`（外链台账、Reddit、Dev.to） | 4 |
| `ledger_tools.py` | `LedgerOps`（台账 upsert、KV、事件、通知） | 9 |
| `promotion_tools.py` | `PromotionOps`（排期读写） | 2 |
| `publishing_tools.py` | `PublishingOps`（页面台账、渲染、push、验活） | 7 |

装配在 `registry.py`：`RuntimeService(6 个 Ops)` 是唯一门面，`ToolRegistry.bind(ops)`
把门面注入 26 个工具。跨域复用写成 `self.<method>`（如 `PublishingOps.queue_indexing`
里调 `self.ledger_upsert`），门面组合后自然可见，不额外 import 其它域的 Ops。

## 6. 适配器

| 子包 | 模块 | 对外接口 |
|---|---|---|
| `cloudflare/` | `client` | `CloudflareClient.url/request/result`（Bearer + 超时） |
| | `database` | `D1Database.query / execute` |
| | `keyvalue` | `WorkersKV.get / put`（键统一 `agent:` 前缀） |
| | `sql` | `Sql` / `PAGE_QUERIES` / `LEDGER_TABLES` / `SqlBuilder` |
| `sources/` | `radar` | `snapshot(status, limit)` → `{items, meta, interests}` |
| | `gsc` | `search_analytics(days, dimensions)`；无凭据返回 `[]` |
| | `reddit` | `search(query, limit, timeframe)` 只读 |
| `hosting/` | `repository` | `ensure_repo / push_files / head_sha / has_staged_changes` |
| | `renderer` | `render_page / render_files`（写 `<asset_dir>/<slug>.html`） |
| | `verification` | `check_runs / wait_build / verify_live` |
| `publishing/` | `devto` | `publish(payload)` |
| | `email` | `send(subject, html)`（Resend） |

### 6.1 SQL 分四层

| 层 | 内容 | 例子 |
|---|---|---|
| `Sql` | 列名与条件写死的语句，参数只有时间窗 / limit | `DAILY_STATS`（按天 PV/UV）、`PENDING_EVENTS` |
| `PAGE_QUERIES` | 查询意图 → `(SQL, window_days)` | `all` / `recent` / `new`（30 天）/ `review`（7 天） |
| `LEDGER_TABLES` | 表 → `LedgerSpec(cols, unique, strategy)` | `agent_pages` 唯一键 `(site, slug)` |
| `SqlBuilder` | 由 `LedgerSpec` 拼 INSERT / UPDATE / UPSERT | `write()` 按 strategy 生成 `DO UPDATE` / `DO NOTHING` |

写入策略三种：`upsert`（冲突覆盖）、`ignore`（冲突丢弃，事件不重复改回 pending）、
`lookup`（先 SELECT 再 UPDATE / INSERT，用于 `agent_backlinks`）。
表名与列名只来自契约白名单，值一律 `?` 占位。

### 6.2 页面渲染与上线

1. `ensure_repo` → `/data/repos/<repo>`：不存在就 `clone --depth 1`（token 走 URL），
   已存在就 `fetch` + `reset --hard origin/<branch>`（**本地改动会被丢弃**）。
2. `render_files` → 校验 slug（`^[a-z0-9][a-z0-9-]{2,60}$`）→ 用
   `templates/site-page.html` 填 11 个占位符（lang / brand / host / slug / title /
   description / h1 / intro / sections / faq / jsonld）→ 写 `<asset_dir>/<slug>.html`
   → 更新 `<asset_dir>/sitemap.xml`（**文件不存在则跳过，不新建**）→ `git add -A`。
3. `push_files` → 无暂存变更直接 `skipped`，否则 `commit -m "add: <slug>"` + `push`。
4. `wait_build` → 只认名字含 `Workers Builds` 的 check run，360s 内每 15s 轮询；
   没成功且有本地副本时做一次空提交重推。
5. `verify_live` → `https://<host>/<slug>.html?v=<ts>` 必须 200 且含预期标题前 24 字符。

## 7. 数据契约

```python
class ExtractionSchema(BaseModel):      # 抽取结果
    target_site: str = ""              # 目标子站，空 = 全站群
    intent: str = "general"
    keywords: list[str] = []
    pages: list[str] = []
    notes: str = ""
    requires_tool: bool = True

class WorkflowResult(BaseModel):       # 一轮产物
    intent, confidence, status, validation_reason, tool_results, tool_errors
```

`render()` 生成一行运营摘要：

```
intent=review_metrics; confidence=0.90; status=success;
validation=Validation passed.; tools=3; errors=0
```

## 8. D1 表

`migrations/0001_agent_tables.sql`（在 tool-proxy 的 D1 库执行，与 `events` 同库）：

| 表 | 用途 | 唯一键 | 策略 |
|---|---|---|---|
| `agent_pages` | 内容页台账 | `(site, slug)` | upsert |
| `agent_reports` | 日报 / 周报 / 推广排期 | `(report_date, kind)` | upsert |
| `agent_backlinks` | 外链台账 | `event_key` | lookup |
| `agent_events` | 事件队列 | `event_key` | ignore |
| `agent_reflections` | 失败复盘 / 效果反思 | `event_key` | upsert |
| `agent_tool_calls` | 工具调用审计（`write_ledger` 可写） | `event_key` | upsert |
| `agent_idempotency` | 幂等键（`write_ledger` 可写） | `idem_key` | upsert |

KV 键统一 `agent:` 前缀，与 radar / console 共用同一个命名空间。

## 9. Checkpointer

| 后端 | 用途 | 说明 |
|---|---|---|
| `memory`（默认） | 本地 / 测试 | 进程重启即丢 |
| `sqlite` | Render 生产 | `DATABASE_URL=sqlite:////data/agent.sqlite`，落持久盘 |

`SqliteSaver.from_conn_string()` 返回的是**上下文管理器**，不是 saver 实例；
直接传给 `compile(checkpointer=...)` 会在第一次写入时炸。这里自行
`sqlite3.connect(path, check_same_thread=False)`（FastAPI 多线程）再构造。

## 10. 可观测性

| 手段 | 实现 |
|---|---|
| 日志 | `utils/logger.py`：单行 JSON（level / logger / message / extra），`propagate=False` |
| 指标 | `observability/metrics.py`：进程内计数器，`/health` 返回快照 |
| 追踪 | `observability/langsmith.py`：`@traceable` 包装器，写 span 日志；`LANGSMITH_TRACING=true` 时才真正上报 |

## 11. 站点配置与模板

`config/sites.json` 一条记录：

```json
{"name": "crcl", "repo": "cockcroft-gault-calculator", "brand": "Cockcroft-Gault Calculator",
 "lang": "en", "branch": "main", "daily_limit": 5, "asset_dir": "public"}
```

| 字段 | 必填 | 缺省 | 用途 |
|---|---|---|---|
| `name` | ✅ | — | 站点标识 = 子域名；工具用它定位站点 |
| `repo` | 否 | 同 `name` | GitHub 仓库名（`GH_ORG/repo`） |
| `subdomain` | 否 | 同 `name` | 子域名；空串表示主域本身 |
| `brand` | 否 | `""` | 页面品牌名 |
| `lang` | 否 | `en` | `<html lang>` |
| `branch` | 否 | `main` | push 目标分支 |
| `daily_limit` | 否 | `5` | 单站每日建页上限（由调用方遵守） |
| `asset_dir` | 否 | `public` | 静态资产目录，必须与仓库 `wrangler.toml` 的 `assets.directory` 一致 |

接入一个新站的前提：仓库是资产目录布局、页面能套 `templates/site-page.html`、
`GITHUB_TOKEN` 对该仓库有 Contents 写权限。
