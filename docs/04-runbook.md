# 04 · 部署与运行手册

> 本地跑、测试、D1 建表、Render 部署、验证清单、排障。命令都可直接复制。

## 1. 本地

```bash
cd site-agent
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env                              # 不填任何凭据也能跑
```

```bash
pytest -q                                          # 47 passed
python -m src.app.main                             # CLI 跑一轮
uvicorn src.app.api.routes:api --reload --port 10000
curl -s localhost:10000/health
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
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` | 否 | 追踪开关，默认关闭 |
| `LLM_API_KEY` | 否 | 留空走确定性回退 |
| `LLM_BASE_URL` | 否 | 兼容任何 OpenAI 风格服务 |
| `LLM_MODEL` | 否 | 默认 `gpt-4o-mini` |
| `FLEET_DOMAIN` | 否 | 站群主域，默认 `toolara.dev`；子站 host / radar / 邮箱都从它派生 |
| `SITES_FILE` | 否 | 站点清单路径，默认 `config/sites.json` |
| `AGENT_STATE_DIR` | 否 | 本地仓库副本根目录，默认 `data/`；生产 `/data` |
| `CF_ACCOUNT_ID` / `CF_API_TOKEN` / `D1_DATABASE_ID` | 是 | D1 读写 |
| `KV_NAMESPACE_ID` | 是 | Workers KV（键 `agent:` 前缀） |
| `GITHUB_TOKEN` / `GH_ORG` | 是 | 仓库 clone / push / check run |
| `RADAR_URL` / `RADAR_USER` / `RADAR_PASS` | 是 | radar 候选词（Basic Auth） |
| `GSC_SA_JSON` / `GSC_PROPERTY` | 否 | 有则读 GSC，无则返回空列表 |
| `DEVTO_API_KEY` | 否 | 无则发布工具返回 `blocked` |
| `RESEND_API_KEY` / `ADMIN_EMAIL` / `MAIL_FROM` | 否 | 通知邮件；缺任一则跳过发送 |
| `CONTACT_EMAIL` | 否 | 默认 `hello@<FLEET_DOMAIN>`，作发件人兜底 |

完整样例见 `.env.example`。

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

1. `pytest -q` → **47 passed**（`FakeRuntime`，不碰任何外部服务）。
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

## 7. 安全

1. **`/run` 无鉴权**：正式环境必须放在网关鉴权或内网之后，不要裸奔公网。
2. **写工具不可逆**：`git_push` / `publish_devto_article` 只能由请求 `tools` 显式触发，
   调用方自己承担后果；不要把 `tools` 的来源接到不可信输入上。
3. **凭据最小化**：`GITHUB_TOKEN` 只给内容读写，`CF_API_TOKEN` 只给 D1 / KV 所需权限。
4. **本地副本会被重置**：`ensure_repo` 对已存在仓库执行 `fetch` + `reset --hard`，
   手工改动会被清掉，调试请另起目录。
