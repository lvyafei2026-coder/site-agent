<!--
Copyright (c) 2026 lvyafei · https://toolara.dev
SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
-->

# 04 · 运维手册

> 本文回答：**怎么把它跑起来、平时怎么操作、出问题怎么查。**
> 面向要部署和维护它的人。原理见 [02-architecture.md](02-architecture.md)，实现见 [03-tech-design.md](03-tech-design.md)。

---

## 1. 凭据清单

部署前必须凑齐这些。**建议按顺序办**，因为后一项依赖前一项的信息。

| # | 凭据 | 从哪拿 | 权限 / 注意 | 必需 |
|---|---|---|---|---|
| 1 | `CF_ACCOUNT_ID` | Cloudflare 控制台 URL 里那串 32 位 hex | — | ✅ |
| 2 | `CF_API_TOKEN` | My Profile → API Tokens → Create Token | **只勾两项**：D1 Edit、Workers KV Storage Edit | ✅ |
| 3 | `D1_DATABASE_ID` | Workers & Pages → D1 → 目标库（tool-proxy 的 CLICKS 库） | 建表要用同一个库 | ✅ |
| 4 | `KV_NAMESPACE_ID` | Workers & Pages → KV → 命名空间 ID | 建议单独建一个给 agent 用，或复用现有 | ✅ |
| 5 | `GITHUB_TOKEN` | Settings → Developer settings → Fine-grained tokens | **仅选子站仓库**，Repository permissions → Contents: Read and write | ✅ |
| 6 | `GH_ORG` | GitHub 用户名或组织名 | 与子站仓库的 owner 一致 | ✅ |
| 7 | `GSC_SA_JSON` | Google Cloud → 创建服务账号 → 下载 JSON → 压成单行 | **必须去 Search Console 把它加为资源所有者**，否则 403 | ✅ |
| 8 | `LLM_API_KEY` | 所选 LLM 服务商控制台 | OpenAI 兼容格式 | ✅ |
| 9 | `IMAP_PASS` | Gmail → 两步验证 → 应用专用密码 | **不是登录密码** | ✅ |
| 10 | `RESEND_API_KEY` | Resend 控制台 → API Keys | 发信域名需先验证 | 建议 |
| 11 | `RADAR_USER` / `RADAR_PASS` | radar 的 Basic Auth 凭据 | 未配则不取候选词，gen-page 会跳过 | 建议 |
| 12 | `DEVTO_API_KEY` | dev.to → Settings → Extensions → DEV API Keys | 未配则 Dev.to 渠道降级为草稿（`blocked`），不影响其他任务 | 可选 |

> **最容易漏的一步是第 7 项**：服务账号建好了、JSON 也拿到了，但没在 Search Console 里添加为资源所有者，API 会一直返回 403，且错误信息不会告诉你是权限问题。

---

## 2. 首次部署

八步，每步都给了验证方法。**不要跳步**。

### 步骤 1 · 装依赖

```bash
git clone https://github.com/<你>/site-agent.git
cd site-agent
uv sync                 # 或 python -m venv .venv && .venv/bin/pip install -e .
```

验证：`uv run python -c "import langgraph, openai; print('ok')"`

首次 `uv sync` 会生成 `uv.lock`，**提交进仓库**（否则 Render 构建无法锁定版本）。

### 步骤 2 · 建 D1 表

在 tool-proxy 的 D1 库里执行 `migrations/0001_agent_tables.sql`：

```bash
# 方式一：控制台
# Cloudflare 控制台 → D1 → 目标库 → Console → 粘贴 SQL → 执行

# 方式二：wrangler
wrangler d1 execute <库名> --remote --file=migrations/0001_agent_tables.sql
```

验证：

```sql
SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_%';
-- 应返回 agent_pages / agent_backlinks / agent_reports
```

### 步骤 3 · 配环境变量

```bash
cp .env.example .env
# 按第 1 节的清单填写
```

本地调试时需 `export $(grep -v '^#' .env | xargs)`，或改用 `direnv` / `python-dotenv`。

### 步骤 4 · 跑单测

```bash
uv run pytest -q test
```

验证：`7 passed`。这 7 项测的是红线校验器与 slug 安全规则，**不依赖任何凭据**，所以这一步必须过。

### 步骤 5 · 最小链路联调

先只跑日报（纯读，零风险）：

```bash
uv run python -c "from site_agent.main import run_task; print(run_task('daily-report'))"
```

验证三件事：

1. 终端日志里能看到 D1 聚合结果的条数（若为 0，多半是 `SQL_DAILY` 的列名与实际 schema 不符，见 §5.1）
2. 邮箱收到 HTML 日报
3. `SELECT * FROM agent_reports ORDER BY id DESC LIMIT 1` 有记录

### 步骤 6 · 建 Render 服务

`render.yaml` 已配好，两种方式：

- **Blueprint**（推荐）：Render → New → Blueprint → 选仓库 → 自动读取 `render.yaml`
- **手动**：New → Background Worker → 选仓库 → 计划选 **Starter** → 加 Disk：name `data`、mount path `/data`、size 1GB

**必须注意两点**：

1. **不能用免费实例**。免费实例的文件系统是临时的，重启后 checkpointer 丢失，所有挂起的人审会静默失效（症状：发了审批邮件，回了 yes，但什么也没发生）。
2. **`AGENT_STATE_DIR` 必须指向 `/data`**，与 disk 的 mount path 一致。

### 步骤 7 · 填 Render 环境变量

把第 1 节的变量逐个填进 Render 的 Environment 页面（或 `render.yaml` 的 `envVars` 用 `sync: false` 占位后在面板填）。

`TZ=Asia/Shanghai` 不要漏，否则 cron 会按 UTC 解释，08:10 会变成北京时间 16:10。

### 步骤 8 · 观察首次启动

部署后在 Render 的 Logs 页面应看到：

```
site-agent 已启动，等待调度（TZ=Asia/Shanghai）
下一次触发的任务：[('gen-page', '2026-11-06 08:10:00+08:00'), ...]
```

如果看到 `缺少环境变量：...` 后进程退出，回步骤 7 补齐。

---

## 3. 环境变量完整表

| 变量 | 必需 | 默认 | 说明 |
|---|---|---|---|
| `CF_ACCOUNT_ID` | ✅ | — | Cloudflare 账户 ID |
| `CF_API_TOKEN` | ✅ | — | 仅需 D1 Edit + KV Edit |
| `D1_DATABASE_ID` | ✅ | — | D1 库 ID（与 events 同库） |
| `KV_NAMESPACE_ID` | ✅ | — | KV 命名空间 ID |
| `GITHUB_TOKEN` | ✅ | — | 细粒度 PAT，Contents: RW |
| `GH_ORG` | ✅ | — | 仓库 owner |
| `LLM_API_KEY` | ✅ | — | LLM 密钥 |
| `LLM_BASE_URL` | 否 | 官方 | 兼容端点，如 `https://xxx/v1` |
| `GSC_SA_JSON` | ✅ | — | 服务账号 JSON 单行 |
| `GSC_PROPERTY` | 否 | `sc-domain:toolara.dev` | GSC 资源标识 |
| `RESEND_API_KEY` | 否 | — | 缺失则只记日志不发信 |
| `ADMIN_EMAIL` | 否 | — | 收件人（日报 / 告警 / 审批） |
| `IMAP_HOST` | 否 | `imap.gmail.com` | 审批回复轮询 |
| `IMAP_USER` / `IMAP_PASS` | 否 | — | 缺失则只能用 CLI 审批 |
| `RADAR_URL` | 否 | `https://radar.toolara.dev` | 候选词来源 |
| `RADAR_USER` / `RADAR_PASS` | 否 | — | radar Basic Auth |
| `DEVTO_API_KEY` | 否 | — | 缺失则 Dev.to 渠道降级为草稿 |
| `AGENT_STATE_DIR` | 否 | `./data` | **Render 上必须是 `/data`** |
| `TZ` | 否 | 系统 | 建议显式 `Asia/Shanghai` |
| `MAX_PAGES_PER_RUN` | 否 | `5` | 单次内容生成上限 |
| `MAX_LLM_CALLS_PER_DAY` | 否 | `100` | 每日 LLM 调用上限 |
| `BREAKER_THRESHOLD` | 否 | `3` | 连续失败几次触发熔断 |
| `BREAKER_COOLDOWN_SEC` | 否 | `1800` | 熔断冷却秒数 |

---

## 4. 日常操作

### 4.1 调度时间表

按北京时间（`TZ=Asia/Shanghai`）：

| 时间 | 任务 | 说明 |
|---|---|---|
| 每天 08:10 | `gen-page` | 读候选词 → 生成内容页 → 待审 |
| 每天 09:00 | `daily-report` | 数据解读日报 + GSC 表现回填 |
| 周一/三/五 10:00 | `backlink` | 外链机会采集与评分 |
| 周日 20:00 | `promo` | 下周推广排期 |
| 每 5 分钟 | `approvals` | 轮询审批邮件回复 |

### 4.2 手动触发任务

```bash
# 任一任务，用当天的 thread_id
uv run python -c "from site_agent.main import run_task; print(run_task('daily-report'))"
uv run python -c "from site_agent.main import run_task; print(run_task('gen-page'))"
```

注意：`thread_id` 是 `"<task>:<当天日期>"`。**同一天重复触发会续跑同一个 thread**，而不是新建——这是刻意的（防重复建页），但意味着"想重跑一次干净的任务"要换日期或删 checkpointer 记录。

### 4.3 本地只跑调度器（不部署）

```bash
uv run python -m site_agent.main
```

本地跑时 cron 用的是本机时间，若不想等，直接用 §4.2 手动触发。

### 4.4 查看状态

```bash
# 有没有挂起等人审的 thread
sqlite3 data/agent.sqlite "SELECT thread_id, COUNT(*) FROM checkpoints GROUP BY thread_id;"

# 今日上线了哪些页面
# （在 D1 控制台执行）
SELECT site, slug, keyword, status, created_at
FROM agent_pages ORDER BY created_at DESC LIMIT 20;

# 最近一次日报
SELECT report_date, summary FROM agent_reports
WHERE kind='daily' ORDER BY id DESC LIMIT 1;
```

### 4.5 审批操作

**正常路径**：收到主题为 `[site-agent] 待确认：<thread_id>` 的邮件，直接回复正文第一行 `yes` 或 `no`（不要带引用原文，虽然代码会切掉，但少一层麻烦）。最多 5 分钟后生效。

**CLI 兜底**（IMAP 不可用、或邮件丢了）：

```bash
uv run python -m site_agent.resume 'gen-page:2026-11-05' yes
uv run python -m site_agent.resume 'gen-page:2026-11-05' no
```

**如果误批了怎么办**：内容是 git 提交，直接去对应子站仓库 `git revert` 那个 commit；台账在 D1，`UPDATE agent_pages SET status='failed' WHERE ...`。

---

## 5. 故障排查

按症状查表。大部分问题在日志里会直接写明原因。

### 5.1 日报收到但数据全是 0

**原因**：`tasks/daily_report.py` 的 `SQL_DAILY` 里的列名与 tool-proxy 实际 `events` 表不符。

**处理**：

```sql
-- 先看实际 schema
PRAGMA table_info(events);
```

对照修改 `SQL_DAILY` 里的 `SELECT` 列名。这是全项目唯一需要按实际环境校准的地方，改一处即可。

### 5.2 gen-page 什么都没做（无动作、无报错）

按顺序排查：

1. **radar 取不到候选词** → 检查 `RADAR_URL` / `RADAR_USER` / `RADAR_PASS`。日志里会有 radar 请求失败的 warning。
2. **候选词都已被做过** → `parse` 里按 `(site, slug)` 去重会全部跳过，日志有"页面已存在，跳过"。
3. **全部命中 DEAD 规则** → 日志有"规则层判死"。
4. **LLM 返回的 site 不在 `config.SITES` 里** → 日志有"丢弃非法页面建议"。当前只配了 5 个 AI 站（music / figurine / creative / background / audio）。

### 5.3 push 成功但线上没变化

这是最常见的坑，按顺序判断：

1. **check run 没出现**（GitHub 侧延迟 30s~4min，多仓库齐推更久）→ 代码会自动空提交重推一次。日志有"构建未成功，空提交重推"。
2. **构建失败** → 去 GitHub 该仓库的 Actions/Checks 页面看 `Workers Builds` 的具体错误。常见：`wrangler.toml` 的 assets 路径不对、仓库 build settings 有问题。
3. **构建成功但页面 404** → 检查写入路径是否与 `wrangler.toml` 的 `assets` 目录一致（代码写的是 `public/<slug>.html`）。
4. **页面 200 但内容还是旧的** → 边缘缓存。代码用 `?v=<时间戳>` 穿透，若仍命中缓存，等 1-2 分钟。

### 5.4 审批邮件发了，回了 yes 没反应

| 可能原因 | 怎么确认 | 处理 |
|---|---|---|
| checkpointer 在临时盘上（用了免费实例） | Render 面板看是否有 Disk | 必须升 Starter + 挂 1GB 盘 |
| IMAP 凭据错 | 日志有"审批轮询失败" | 用应用专用密码，不是登录密码 |
| 回复正文没被识别成 yes/no | 日志没有"处理审批回复" | 回复内容只写 `yes`，不要写"好的可以" |
| thread 已结束 | `sqlite3` 查不到该 thread | 动作已执行或被拒，看台账确认 |

**兜底**：直接用 CLI（§4.5），这条路径不依赖 IMAP。

### 5.5 任务被熔断跳过

日志：`⏸ 任务被熔断跳过 <thread>：...`

```bash
# 查熔断键（KV 控制台或 wrangler）
wrangler kv key get --namespace-id <NS> "agent:pause:gen-page"

# 立即恢复：删键
wrangler kv key delete --namespace-id <NS> "agent:pause:gen-page"
```

恢复前先看日志里连续 3 次失败的具体原因——熔断只是结果，别重复同样的失败。

### 5.6 LLM 调用全部失败

1. `LLM_API_KEY` 是否有效、余额是否够
2. `LLM_BASE_URL` 是否写对（兼容端点通常要以 `/v1` 结尾，官方留空）
3. 模型名是否可用——`PROFILES` 里写的是 `gpt-4o-mini`，用别家的端点要改成对应模型名

### 5.7 GSC 返回 403

**几乎一定是服务账号没被加为 Search Console 的资源所有者**。去 Search Console → 设置 → 用户和权限 → 添加用户 → 填服务账号邮箱（JSON 里的 `client_email`）→ 权限选"完全"或"受限"。

### 5.8 进程反复重启

看 Render 的 Logs：

| 日志特征 | 原因 |
|---|---|
| `MemoryError` / OOM killed | 是不是装了 playwright？单任务并发了？ |
| `缺少环境变量` | 变量没填全 |
| `database is locked` | 多个实例同时跑（确认 instances = 1） |
| `ModuleNotFoundError` | `uv.lock` 没提交，或 `pyproject.toml` 改了没加依赖 |

### 5.9 git push 失败

| 错误 | 原因 | 处理 |
|---|---|---|
| `Authentication failed` | PAT 过期或权限不足 | 重新生成，Contents: Read and write |
| `could not read Username` | 交互式提示（无 TTY） | 代码已设 `GIT_TERMINAL_PROMPT=0`，检查 token 是否被注入 URL |
| `non-fast-forward` | 工作副本与远端分叉（有人手改了仓库） | 代码用 `fetch + reset --hard` 自动纠正，若仍失败手动去 `/data/repos/<repo>` 处理 |

---

## 6. 升级与回滚

### 6.1 升级依赖

```bash
# 改 pyproject.toml 里的版本约束
uv lock --upgrade-package langgraph
uv sync
uv run pytest -q test        # 必须过
git add uv.lock pyproject.toml && git commit -m "chore: bump langgraph"
```

**LangGraph 升级要格外小心**：`interrupt` / checkpointer 的 API 在 0.x 期间改过多次。升级后先用 `run_task('daily-report')` 跑一遍全链路（纯读，安全），再等 cron。

### 6.2 回滚

```bash
git revert <bad-commit>
git push        # Render 自动重新部署
```

**注意**：若新版本改过 checkpointer 表结构，回滚后旧版本可能读不了新写入的 checkpoint。这种情况需要清掉 `/data/agent.sqlite`（会丢挂起的人审，但已执行的动作记录在 D1 里，不会丢）。

### 6.3 清空状态重来

```bash
# Render Shell，或本地
rm /data/agent.sqlite
# 进程重启后 checkpointer 会重建空库
```

D1 里的台账**不受影响**（那是唯一真相源）。

---

## 7. 定期维护

| 周期 | 做什么 | 怎么看 |
|---|---|---|
| 每天 | 看日报邮件；处理待审批邮件 | 邮件 |
| 每周 | 看 `agent_pages` 新页的收录情况 | GSC 后台 + `agent_pages` |
| 每周 | 抽查 1-2 个新生成页面质量 | 直接打开线上页面 |
| 每月 | 看 LLM 花费（token 累计） | 日报里的成本行 / 服务商账单 |
| 每月 | 复查外链存活（30 天到期的会自动查） | `SELECT * FROM agent_backlinks WHERE status='dead'` |
| 每季 | 复盘：哪些词产出的页面真的有流量 | join `agent_pages` 与 GSC 数据 |

**前 20 个生成页面必须人工全看一遍**。这是建立质量基线的唯一办法——模型跑偏时不会报警，只会稳定地产出"看起来像但没用"的内容。

---

## 8. 安全操作约束

这些是**不要做**的事，做了会破坏边界：

- ❌ 不要给 `CF_API_TOKEN` 加 Workers Scripts: Edit 权限（Agent 不需要改站点代码）
- ❌ 不要给 `GITHUB_TOKEN` 开 `admin` 或 `delete` 权限
- ❌ 不要把 `.env` 提交进仓库（`.gitignore` 已排除，但用 `git add -f` 能绕过）
- ❌ 不要手动往 `events` / `candidates` 表写数据（那是 tool-proxy 与 radar 的领地）
- ❌ 不要在 Render 上把 instances 调成 > 1（checkpointer 会锁冲突）
- ❌ 不要把 `AGENT_STATE_DIR` 指向非持久路径

---

## 9. 延伸阅读

- 为什么要 Starter + 磁盘 → [02-architecture.md §6](02-architecture.md#6-部署拓扑)
- 审批通道的实现细节 → [03-tech-design.md §5](03-tech-design.md#5-人审中断)
- 分阶段计划与验收标准 → [05-roadmap.md](05-roadmap.md)
