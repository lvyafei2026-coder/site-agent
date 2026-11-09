# site-agent

toolara.dev 站群的运营 Agent：一句运营请求跑完一轮
**判意图 → 抽字段 → 调工具取事实 → 校验 → 出报告**，需要时把结论写进 D1 台账。

Python 3.11+ / LangGraph / FastAPI，部署在 Render，外部系统只有 Cloudflare D1/KV、
GitHub、radar、GSC、Reddit / Dev.to、Resend 与一个 OpenAI 兼容 LLM。

## 当前形态

单轮流水线，五个节点：

```
POST /run → classify_intent → run_processing → validate_result
                                        ↑            │
                                        └── retry ───┤
                                                     ├─ 通过 → build_output
                                                     └─ 重试耗尽 → handle_error
```

25 个工具全部是 `BaseTool` 子类，配 Pydantic `Input`，统一返回 `ToolOutput`：
13 个 `read`、10 个 `ledger`、2 个 `write`。所有外部 IO 只发生在 `tools/adapters/`。

| 记住这一条 | 含义 |
|---|---|
| **只读默认** | 默认编排只调 `read` 工具；`write`（`git_push`、`publish_devto_article`）必须由请求 `tools` 显式列出 |
| **写动作靠显式声明** | 不保存待审状态；`write` 工具必须由请求 `tools` 列出，调度交给外部 cron |
| **无 LLM 也能跑** | 没有 `LLM_API_KEY` 时意图回退 `general`、抽取走本地正则，图照样跑完 |
| **工具失败不抛异常** | 单个数据源挂了只记 `ok=false`，由校验环节决定重试还是报错 |

## 快速开始

```bash
pip install -e .
cp .env.example .env
python -m src.app.main                 # CLI 跑一轮
uvicorn src.app.api.routes:api --reload --port 10000
pytest -q                              # 47 passed
```

```bash
curl -s -X POST localhost:10000/run -H 'Content-Type: application/json' \
  -d '{"input_text":"复盘近期点击","intent":"review_metrics"}'
```

`intent` 可选 `select_pages` / `plan_promo` / `find_backlinks` / `review_metrics` / `general`；
不传则由 LLM 分类。请求还能传 `tools`（覆盖默认工具集）、`prompt_versions`
（如 `{"intent":"v2"}`）、`thread_id`。

## 目录

```
src/app/
├── main.py / config.py      CLI 入口 / Settings 单例 + 站群清单 + 意图枚举
├── api/routes.py            FastAPI /run /health
├── graph/                   builder · state · constants(Names/Tags) · edges
├── nodes/                   intent · processing · validation · output · error
├── services/                llm · prompt · tool · evaluation
├── tools/
│   ├── base.py              契约（BaseTool / RuntimeTool / ToolOutput）
│   ├── registry.py          门面装配（RuntimeService）+ 25 个工具注册
│   ├── *_tools.py           6 个业务域：<Domain>Ops 实现 + 工具类
│   └── adapters/            cloudflare · sources · hosting · publishing
├── models/                  ExtractionSchema · WorkflowResult
├── prompts/versions/        <task>/vN.txt（intent 有 v1/v2，extraction 有 v1）
├── memory/checkpointer.py   memory / sqlite
├── llm/provider.py          OpenAI 兼容客户端
├── observability/           metrics · langsmith
└── utils/                   logger（单行 JSON）
config/sites.json            32 个站点的清单（配置，不是代码）
templates/site-page.html     内容页模板（11 个占位符）
migrations/0001_agent_tables.sql  D1 台账表
```

## 关键约束

1. 依赖单向：`api → graph → nodes → services → tools → adapters`；
   **tools 不依赖 services**，`tools/base.py` 只依赖标准库与 pydantic，
   工具层没有函数内 import。三条都由 `test/test_architecture.py` 守着。
2. 门面靠构造注入：`RuntimeService` 在 `registry.py` 组装后注入 25 个工具，
   测试用 `tool_registry.bind(FakeRuntime())` 整体替换。
3. 站点信息只在 `config/sites.json`；域名只在 `FLEET_DOMAIN`。
4. SQL 只在 `tools/adapters/cloudflare/sql.py`；表名列名只来自白名单契约。
5. `/run` 没有鉴权，上线前必须在网关层加访问控制。

## 文档

| 文档 | 内容 |
|---|---|
| [01-overview](docs/01-overview.md) | 服务对象、一轮做什么、不做什么、风险 |
| [02-architecture](docs/02-architecture.md) | 分层与依赖、装配、运行时序、部署拓扑、边界、扩展点 |
| [03-tech-design](docs/03-tech-design.md) | 状态契约、图、节点、服务、工具、适配器、SQL、表结构 |
| [04-runbook](docs/04-runbook.md) | 本地 / 测试 / D1 建表 / Render / 排障 / 安全 |
| [05-tools](docs/05-tools.md) | 25 个工具速查、意图映射、站点清单、新增清单 |
| [10-structure](docs/10-structure.md) | 结构唯一口径（文件级） |
| [glossary](docs/glossary.md) | 术语表 |

## 协议

PolyForm-Noncommercial-1.0.0，署名 lvyafei。可学习、研究、修改与非商业使用，禁止商业使用。
