# 04 · 部署与运行手册

> 本地跑、测试、D1 建表、Render 部署、验证清单、排障。命令都可直接复制。

## 1. 本地

项目由 `langgraph-init-cli` 0.1.5 的 `production` 模板生成（来源与改造清单见
[10-structure.md §1](10-structure.md)）。**不要在现有目录重跑 `langgraph-init`**——
CLI 没有 upgrade，重跑会覆盖同名文件。

```bash
cd site-agent
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env                              # 不填任何凭据也能跑
```

```bash
pytest -q                                          # 56 passed
python -m src.app.main                             # CLI 跑一轮
uvicorn src.app.api.routes:api --reload --port 10000
curl -s localhost:10000/health
curl -s localhost:10000/llm-quota                  # LLM 剩余额度（每次消耗 1 次请求额度）
curl -s -X POST localhost:10000/run \
  -H 'Content-Type: application/json' \
  -d '{"input_text":"复盘近期点击","intent":"review_metrics"}'
```

无 `LLM_API_KEY`：意图回退 `general`、抽取走本地正则，图仍跑完。
无 Cloudflare / GitHub 凭据：工具返回 `ok=false` → 校验不通过 → 重试 →
`handle_error`，属预期行为（`output` 以 `ERROR:` 开头）。

请求体全部字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `input_text` | ✅ | 运营请求原文 |
| `intent` | 否 | 强制指定意图，跳过 LLM 分类（`confidence=1.0`） |
| `tools` | 否 | 显式工具列表，**覆盖**意图默认集；写工具只能这样触发 |
| `prompt_versions` | 否 | 如 `{"intent": "v2"}`，缺省回退 v1 |
| `thread_id` | 否 | checkpointer 会话归属，缺省 `run-<毫秒>` |

返回值是完整 state：`output` / `error` / `intent` / `confidence` /
`extracted_data` / `tool_results` / `validation` / `messages`。

## 2. 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `LOG_LEVEL` | 否 | 默认 `INFO`；日志是单行 JSON |
| `MEMORY_BACKEND` | 否 | `memory`（默认）/ `sqlite` |
| `DATABASE_URL` | sqlite 时 | 生产用 `sqlite:////data/agent.sqlite` |
| `MAX_VALIDATION_RETRIES` | 否 | 默认 2（最多 3 次取数） |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` | 否 | 追踪开关，默认关闭；见 §2.2 |
| `LANGSMITH_PROJECT_URL` | 否 | 项目页地址，用于 console 的结果回链与热词批次 `traceRun.url`；不填会自动解析；见 §2.2 |
| `LLM_API_KEY` | 否 | 留空走确定性回退 |
| `LLM_BASE_URL` | 否 | 兼容任何 OpenAI 风格服务 |
| `LLM_MODEL` | 否 | 默认 `gpt-4o-mini`；生产用 `openai/gpt-4o-mini`（见下方「换模型」） |
| `FLEET_DOMAIN` | 否 | 站群主域，默认 `toolara.dev`；子站 host / radar / 邮箱都从它派生 |
| `SITES_FILE` | 否 | 站点清单路径，默认 `config/sites.json` |
| `AGENT_STATE_DIR` | 否 | 本地仓库副本根目录，默认 `data/`；生产 `/data` |
| `CF_ACCOUNT_ID` / `CF_API_TOKEN` / `D1_DATABASE_ID` | 是 | D1 读写 |
| `KV_NAMESPACE_ID` | 是 | Workers KV（★热词分析必须）：与 radar / console 共用同一个 RADAR_KV 命名空间，键与 console/radar 对齐（`ai:v3:` / `serp:v1:` / `trend:v1:` / `saved` / `rejected` 等），agent 经 Cloudflare KV REST API 直读直写，不调它们的 App 接口 |
| `BACKFILL_TOKEN` | 否 | `POST /backfill` 的请求头 `X-Backfill-Token`；不配则与 `/run` 一样无鉴权（上线必须配） |
| `GITHUB_TOKEN` / `GH_ORG` | 是 | 仓库 clone / push / check run |
| `GSC_SA_JSON` / `GSC_PROPERTY` | 否 | 有则读 GSC，无则返回空列表 |
| `DEVTO_API_KEY` | 否 | 无则发布工具返回 `blocked` |
| `RESEND_API_KEY` / `ADMIN_EMAIL` / `MAIL_FROM` | 否 | 通知邮件；缺任一则跳过发送 |
| `CONTACT_EMAIL` | 否 | 默认 `hello@<FLEET_DOMAIN>`，作发件人兜底 |

**换模型**：候选模型看 <https://github.com/marketplace?type=models>（GitHub Models 目录，
匿名可打开；单模型详情页如 `/marketplace/models/azure-openai/gpt-4o-mini` 需登录）。
注意两套写法不同：目录 20 个模型的详情页路径是 `azure-openai/gpt-4o-mini`，而推理
API 的 `LLM_MODEL` 要写 **`openai/gpt-4o-mini`**（vendor/model）——以模型详情页里
代码示例的 `model` 字段为准，别照抄页面路径。改完在 Render 环境变量里更新
`LLM_MODEL` 触发重建即可，`/llm-quota` 会显示当前实际生效的模型名。

完整样例见 `.env.example`。

### 2.1 参数从哪来（照抄值 / 找值位置）

| 变量 | 值 | 来源 |
|---|---|---|
| `FLEET_DOMAIN` | `toolara.dev` | 站群主域；子站 host、radar 地址、邮箱全由它派生 |
| `GH_ORG` | `lvyafei2026-coder` | GitHub 用户名（token 属于哪个账号就填哪个） |
| `D1_DATABASE_ID` | `07a8858a-b269-4680-9059-b336ccf6fc4b` | tool-proxy / site-console 共用的那个 D1（CLICKS 库）；agent 的 7 张 `agent_*` 表也建在它上面 |
| `KV_NAMESPACE_ID` | `3cb9c51a017748319ae9924b08ded64c` | 就是 RADAR_KV；agent 与 radar / console 共用同一个 namespace，键也对齐（`ai:v3:` / `serp:v1:` / `trend:v1:` / `saved` / `rejected` 等），agent 经 Cloudflare KV REST API 直读直写 |
| `ADMIN_EMAIL` / `CONTACT_EMAIL` | `hello@toolara.dev` | 域名邮箱；`CONTACT_EMAIL` 缺省也是 `hello@<FLEET_DOMAIN>` |

找值位置：

- **D1 id**：Cloudflare Dashboard → Storage & Databases → D1 → `web-clicks-analytics` → Settings → Database ID
- **KV id**：Cloudflare Dashboard → Storage & Databases → Workers KV → namespace `RADAR_KV`
- **域名邮箱**：Cloudflare Email Routing 里配置的 `hello@toolara.dev`

其余全是凭据，只进 Render 的 Environment，**不写进文档、不提交仓库**：
`CF_ACCOUNT_ID` / `CF_API_TOKEN` / `KV_NAMESPACE_ID` / `GITHUB_TOKEN` /
`LLM_API_KEY` / `RESEND_API_KEY` / `DEVTO_API_KEY` / `GSC_SA_JSON`。
（`RADAR_USER` / `RADAR_PASS` / `CONSOLE_PASS` / `CONSOLE_HQR` 已随去接口化删除——
本服务不再调 radar / console 的任何接口，只直读直写共享 KV。）

### 2.2 LangSmith 追踪（怎么看每轮跑了什么）

**它解决什么**：`/run` 返回的 JSON 只有最终 state，看不到「意图凭什么判成 select_pages」
「哪个工具慢」「重试那一轮模型收到什么 prompt」。打开追踪后每一轮都是一棵可展开的
调用树：图级 run → 各节点 → LLM 请求（含完整 messages、token 数、耗时）。

**三步开启**

1. 注册 https://smith.langchain.com（GitHub 账号登录即可，免费档 5k traces/月），
   在 **Settings → API Keys → Create API Key** 生成 `lsv2_pt_...`，同时记下项目名
   （默认 `default`，建议新建 `site-agent`）。
2. 本地 `site-agent/.env`：

   ```ini
   LANGSMITH_TRACING=true
   LANGSMITH_API_KEY=lsv2_pt_xxx
   LANGSMITH_PROJECT=site-agent
   # 只有欧盟区实例才需要改：https://eu.api.smith.langchain.com
   LANGSMITH_ENDPOINT=https://api.smith.langchain.com
   ```

3. Render：Environment 里同样加这三项（`render.yaml` 已声明为 `sync: false`，
   在 Dashboard 填值即可；手工加也行），保存后服务会自动重启生效。
   **不要**把 key 写进仓库或 `render.yaml`。

**开关判定**：`LANGSMITH_TRACING=true` 且 `LANGSMITH_API_KEY` 非空才上报（见
`src/app/observability/langsmith.py`）。只有开关没有 key 时按关闭处理——否则每次
调用都 401，日志会被刷满。关闭时 `traceable` 直接返回原函数，零开销。

**打开后在哪看**：smith 项目页 → Runs 列表，每轮一条 `site-agent:<intent>`（未显式
指定意图时是 `site-agent:auto`）。点进去看到：

| 层级 | 内容 | 来源 |
|---|---|---|
| 图级 | 整轮输入输出、总耗时、thread_id、input_text | `routes.py` 的 `run_name` / `metadata` |
| 节点级 | classify_intent / run_processing / validate_result / build_output | LangGraph 自动打点 |
| 工具级 | `tool:<工具名>`（26 个 RuntimeTool）：真实入参、统一返回体，`ok=False` 标红 | `tools/registry.py` 注入的 `trace_tool` |
| 批次级 | `site-agent:hotword-batch`：analyzed / failed / successRate / 降级提示 | `hotwords.run_batch` 的 `run_traced` |
| 批次内 IO | `tool:kv.read-candidates`：筛出哪些词、为什么这批跑空了 | `hotwords._run_batch` 的 `trace_tool` |
| 逐词级 | `site-agent:hotword:<词>`：verdict / status / serp / trend / error（失败标红） | `hotwords._run_batch` 的逐词 `run_traced` |
| 逐词内 IO | `tool:serp` / `tool:trend` / `tool:kv.write`：抓取出入参、缓存命中、尝试明细、每个 KV 键写入成败 | `hotwords.py` 的 `_tool_step` |
| LLM 级 | 实际发送的 messages、返回 JSON、token 数与延迟 | `llm/provider.py` 的 `wrap_openai` |

**整轮是一棵树，不是一堆散根**（2026-11-13 修）：

```
site-agent:analyze_hotwords                    ← 唯一根
└─ run_processing
   ├─ tool:get_hotwords / tool:get_page_inventory …   ← 26 个工具各自的出入参
   └─ site-agent:hotword-batch
      ├─ tool:kv.read-candidates
      └─ site-agent:hotword:<词>              ← 每词一条
         ├─ tool:serp      → SERP 前 10、竞争度、是否命中缓存、4 发尝试明细
         ├─ tool:trend     → 趋势点数/摘要、是否命中缓存
         ├─ ChatOpenAI     → 完整 messages 与返回
         └─ tool:kv.write  → 写了哪些键、每个键 ok 与否、正文预览、失败清单
```

根因是 `ThreadPoolExecutor` 的 worker 线程**不继承主线程的 contextvars**，而 LangSmith
把父 run 存在 contextvar 里；并发逐词时 `wrap_openai` 找不到父，每个 LLM 都成了独立根
trace。修法在工具层：批次 span 自动挂到 LangGraph 节点下，逐词 span **显式传 `parent=`**
（`observability/langsmith.py:run_traced`），并把本词 run 继续传给所有 `tool:*` 子 span。
**graph 结构本身没有改动**，也不是「每词一条独立 trace」。

**工具可见性**（2026-11-13）：以前只有 `wrap_openai` 自动打点，SERP / Trends / KV 读写在
Smith 上完全看不见，页面只显示「SERP 源不可用，本批已跳过 SERP」这类结果文字，无法判断
到底调用过没有、卡在哪一步。现在每个工具调用都是一条 `run_type=tool` 的 span：
- 源级故障（`SourceUnavailable`）会让 `tool:serp` / `tool:trend` **标红并带完整 traceback**，
  业务侧照常降级；后续词走「本批已跳过」分支时也会留一条 `skipped` span，不会凭空消失。
- `tool:kv.write` 的 Outputs 里逐个列出 `ai:v3:` / `aihints` / `saved` / `rejected` 的
  key 与 `ok`，排查「正文写了、状态没写」不用再翻日志。
- 26 个 RuntimeTool 连 Input 校验失败（参数缺失/类型错）也有 span 并标红，因为追踪包在
  `RuntimeTool.run` 最外层；tracer 由 `registry.py` **注入**，`base.py` 仍是纯叶子模块。
- 出入参统一经过 `compact_io` 裁剪：单字符串最多 4000 字符、单容器最多 60 项、最多
  6 层，过长会标注原长度并截断，避免整张候选表/AI 正文把上报拖慢或撑爆 UI。
- 关闭追踪时 `trace_tool` 就是原函数调用，`compact_io` 与 langsmith runtime 都不执行。

- 整批共用一个 `thread_id`（= `batchId`），Threads 视图同样聚成一条；每词另有
  独立 `word_id`，要单独看某个词按它检索即可。
- `analyze_hotwords` 返回里新增 `traceRun{batchId, runId, traceId, url}`（附加字段，
  老前端忽略不受影响）。`url` 只在配了 `LANGSMITH_PROJECT_URL` 时生成，不额外发网络；
  没配就用 `runId` 在 LangSmith 搜索框定位。
- 关掉追踪时这层包装零开销：直接调函数，返回里没有 `traceRun`。
- 回归护栏：`test/test_hotword_tracing.py`（含真实 LangGraph 节点场景）验证
  「批次挂节点、每词挂批次、LLM 挂对应词」。

**排障**：列表里一条都没有时，按顺序查 ① 两个环境变量是否都生效
（`/health` 看不到，直接 `docker run` 或 Render Shell 里 `env | grep LANGSMITH`）；
② key 是否属于当前 endpoint 所在的区（EU key 打 US 端点必然 401）；
③ 日志里搜 `Failed to send` / `401`——上报失败只打 warning，不会让 `/run` 失败，
所以**业务成功 ≠ 追踪成功**。

**不接 LangSmith 也能看轨迹**：`/run` 的返回体自带 `trace` 字段（见
`observability/trace.py`），`path` 是一轮的节点序列、`nodes` 是每步耗时与
写入字段、`route` 是 complete/error、`retried` 表示是否重试过。console 的
「Agent入口」页把它渲染成芯片流程图 + 每步变化表，排查不用登 LangSmith。

**LLM 额度**：agent 提供 `GET /llm-quota`，发一次 `max_tokens=1` 的 ping
探测可用性并读回 `x-ratelimit-*` 响应头：200 → 「可用」，429 → 读出
`retry-after` 等待秒数与剩余量。console「Agent入口」页额度卡点击「查询额度」
即查（每次消耗 1 次请求额度）。注意部分网关 200 响应不带限流头，此时 quota
为空属预期，不代表无额度。当前接 DeepSeek 官方 API（标准 OpenAI 兼容，
`LLM_BASE_URL=https://api.deepseek.com` / `LLM_MODEL=deepseek-flash`），
余额请在 <https://platform.deepseek.com> 控制台查看。

**结果页回链**：`/run` 同时带回 `langsmith{enabled, project, url}`，console 在
**每轮结果标题右侧**和**历史行「追踪」列**渲染 `LangSmith ↗`，点开即进项目 Runs。

| 情况 | 按钮 |
|---|---|
| 追踪开着 + 解析成功 | 显示，直达项目页（按 `thread_id` / 时间找这一轮） |
| 追踪开着 + 解析失败 | 不显示（不产生死链） |
| 追踪关闭 | 不显示 |

地址优先取 `LANGSMITH_PROJECT_URL`；不填且追踪开着时，服务端自动向 LangSmith 解析
一次（`observability/langsmith.py`：2s 超时、成功缓存 1 小时、失败缓存 5 分钟并
静默降级，绝不影响 `/run`）。解析走的是 langsmith SDK 的 `read_project` +
`_host_url` / `_get_tenant_id`，**升级 langsmith 大版本后若链接消失**，手填
`LANGSMITH_PROJECT_URL` 即可兜底（smith 项目页 → 复制浏览器地址）。

## 3. D1 建表

```bash
# 在 tool-proxy 的 D1 库执行（与 events 同库，便于 join）
wrangler d1 execute <DB_NAME> --remote --file=migrations/0001_agent_tables.sql
```

建 7 张表：`agent_pages` / `agent_reports` / `agent_backlinks` / `agent_events` /
`agent_reflections` / `agent_tool_calls` / `agent_idempotency`。
checkpointer 的表由 `langgraph-checkpoint-sqlite` 自动创建，不要手工改。

## 4. Render

`render.yaml` 是 Blueprint，直接 New → Blueprint 指向本仓库即可：

| 项 | 值 |
|---|---|
| 计划 | `free`（文件系统临时；单轮流水线无跨轮状态，重启无损失） |
| 构建 | `pip install -e .` |
| 启动 | `uvicorn src.app.api.routes:api --host 0.0.0.0 --port $PORT` |
| 健康检查 | `/health` |
| 关键 env | `MEMORY_BACKEND=memory`、`FLEET_DOMAIN`、`GH_ORG` |

要 checkpoint 持久化时升级：`plan: starter` + Disk（1GB 挂 `/data`），
env 改 `MEMORY_BACKEND=sqlite`、`DATABASE_URL=sqlite:////data/agent.sqlite`、
`AGENT_STATE_DIR=/data`（约 $7/月）。

`sync: false` 的变量（Cloudflare / GitHub / LLM / radar 凭据）在 Render 控制台填。
`langgraph.json` 指向 `src.app.main:run`，供 LangGraph CLI 使用；线上托管以
Render 的 FastAPI 为准。

## 5. 验证清单

1. `pytest -q` → 全绿（`FakeRuntime`，不碰任何外部服务；具体条数随用例增减，别写死）。
2. `python -m compileall -q src test` 通过。
3. `python -m pyflakes src test` 无输出。
4. `/health` 返回 `{"status":"ok","metrics":{...}}`。
5. `/run` 传 `intent=review_metrics` 返回含 `output` 的完整 state。
6. 有凭据时抽查一个只读工具：`tools: ["get_hotwords"]` 应返回 `meta.updatedMs`。

## 6. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 所有工具 `ok=false` | 缺 Cloudflare / GitHub / radar 凭据 | 配 env；本地可接受（图会走 error 分支） |
| `intent` 恒为 `general` | 未配 `LLM_API_KEY`，或 LLM 输出非法意图 | 配 key，或请求里显式传 `intent` |
| 日志 `prompt_loaded version=v1` 但想要 v2 | 请求没传 `prompt_versions` | 传 `{"intent":"v2"}` |
| `error: unknown tool` | `tools` 里写了未注册的工具名 | 对照 `05-tools.md §1` 的 name |
| `git clone` 失败 | `GITHUB_TOKEN` 无该仓库权限 | 检查 token scope（Contents: RW） |
| `wait_build` 返回 `detail: CF 构建失败（已重推一次）` | 构建真的失败 | 去仓库 Actions / CF 面板看日志 |
| `MEMORY_BACKEND=sqlite` 报错 | `/data` 未挂载或不可写 | 检查 Render disk 配置 |
| 页面渲染成功但线上 404 | 仓库 `wrangler.toml` 的 `assets.directory` 与 `asset_dir` 不一致 | 改 `config/sites.json` 的 `asset_dir` |
| sitemap 没更新 | 站点仓库里没有 `<asset_dir>/sitemap.xml` | 补一个空的 `<urlset>` 文件即可 |
| `duckduckgo 不可用：连不上 DuckDuckGo（N 发全部连接失败）` | **网络层**：当前部署出口 IP 到 DDG 不通，与请求头 / 202 挑战页无关。本机直连与另一云端出口实测均 200 + 10 条真结果，Render singapore 出口 4 发全 ConnectTimeout | 默认已自动经公共 Reader 中转换出口（`SERP_FALLBACK_READER=1`，取回原始 DDG HTML，口径一致）；仍失败再配 `SERP_HTTPS_PROXY` 自备代理，或把 `region` 换区重建。★11-13 起源级故障**不再中止整批**：本批跳过 SERP、LLM 照常出结论并回写状态，`items[].notice` 会标「SERP 未取到」（缺辨形线索），返回里 `serpDisabled=true`。4 发已轮换 4 个不同域名 / IP，仍全挂就别再调请求头了 |
| `duckduckgo 不可用：连续 N 发未返回可用结果` | **HTTP 层**：202 anomaly 挑战页。显式设置 `Accept` / `Accept-Language` / `Referer` 任意一个都会触发 | `BASE_HEADERS` 必须只有 `User-Agent`；`test_fetch_serp_uses_only_user_agent` 是防回归测试，别把「更像浏览器」的头加回来 |

### 6.1 热词并发写入与状态回填（2026-11-13）

- 20 条热词由 4 个 worker 并发分析。`saved` / `rejected` / `aihints` / `competition` /
  `interests` 这 5 张**整表**的「读 → 改 → 写 → 校验返回值」统一走
  `sources/kv.py:atomic_update()` 的一把进程级 `threading.RLock`，不会互相覆盖。
  ★只保证单进程：Cloudflare KV 无 CAS 且最终一致，多实例 / console 人工同时写仍可能
  丢更新，靠下面的 error + 回填兜底。
- 每次 `put_text` / `put_json` 都检查返回值，失败写进 `items[].error` + `writeErrors`，
  该词**不算 analyzed**。正文写失败时直接收手，绝不写状态（不制造「有状态、没正文」）。
- 正文已落库但状态整表没写上的词，`_select_pending` 会因「已有正文」永久剔除它。
  用 `POST /backfill` 从正文第 7/8 节反推结论补写（不调 LLM、不重抓数据）：

  ```bash
  # 1) 先看报告（默认 dry_run=true，不写库）
  curl -X POST https://<host>/backfill -H 'Content-Type: application/json' -d '{}'
  # 2) 确认后真正补写
  curl -X POST https://<host>/backfill \
       -H 'Content-Type: application/json' -H "X-Backfill-Token: $BACKFILL_TOKEN" \
       -d '{"dry_run": false, "limit": 200}'
  ```

  结论「观望」按设计不写状态；正文为空 / 结论解析不出的词列在 `items`（`action=unresolved`），
  要重跑得先删掉对应的 `ai:v3:` 键。也可直接在 Python 里调
  `hotwords.backfill_states(kv=..., dry_run=False)`。

## 7. 安全

1. **`/run` 与 `/backfill` 无鉴权**：正式环境必须放在网关鉴权或内网之后，不要裸奔公网；
   `/backfill` 额外支持 `BACKFILL_TOKEN` + `X-Backfill-Token` 校验（默认 `dry_run=true` 只出报告）。
2. **写工具不可逆**：`git_push` / `publish_devto_article` 只能由请求 `tools` 显式触发，
   调用方自己承担后果；不要把 `tools` 的来源接到不可信输入上。
3. **凭据最小化**：`GITHUB_TOKEN` 只给内容读写，`CF_API_TOKEN` 只给 D1 / KV 所需权限。
4. **本地副本会被重置**：`ensure_repo` 对已存在仓库执行 `fetch` + `reset --hard`，
   手工改动会被清掉，调试请另起目录。
