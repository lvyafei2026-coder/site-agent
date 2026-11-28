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

31 个工具全部是 `BaseTool` 子类，配 Pydantic `Input`，统一返回 `ToolOutput`：
13 个 `read`、10 个 `ledger`、2 个 `write`。所有外部 IO 只发生在 `tools/adapters/`。

| 记住这一条 | 含义 |
|---|---|
| **只读默认** | 默认编排一般只调 `read` 工具；`write`（`git_push`、`publish_devto_article`）必须由请求 `tools` 显式列出 |
| **候选池分析不是工具** | 它是编排：独立入口 `POST /hotwords` 走 `graph.run_hotwords()`（逐词循环图），不经过工具注册表。★上线前必须加访问控制 |
| **建页不是工具** | 同为编排：独立入口 `POST /page` 走 `graph.run_page_build()`（plan→render→push→verify 串形子图）；`confirm=false` 是 dry-run，一个写动作都不发生 |
| **写动作靠显式声明** | 不保存待审状态；`write` 工具必须由请求 `tools` 列出，候选池分析的状态写入由热词图自身负责 |
| **无 LLM 也能跑** | 没有 `LLM_API_KEY` 时意图回退 `general`、抽取走本地正则，图照样跑完；候选池分析拿不到 LLM 正文会跳过该词、不编造结论 |
| **工具失败不抛异常** | 单个数据源挂了只记 `ok=false`，由校验环节决定重试还是报错 |

## 快速开始

```bash
pip install -e .
cp .env.example .env
python -m src.app.main                 # 常驻 HTTP：/run 用户线 + /hotwords 热词线 + /page 建页线
pytest -q                              # 全量单测（含 test_hotwords.py / test_page_build.py）
```

`python -m src.app.main` 直接进 `serve()`：只起 uvicorn，三条业务线各是一个 HTTP
入口——`POST /run`（用户线主图）、`POST /hotwords`（热词逐词循环图）、
`POST /page`（建页子图）。**没有后台
线程、没有定时任务**：候选池分析由前端按需触发，跑完即返回（一轮 1-2 分钟）。
本地想看一轮结果直接 `curl -X POST localhost:10000/run`（热词同理 `POST /hotwords`）——
进程里只有 HTTP 入口，没有第二个「单跑一轮」的函数。

```bash
curl -s -X POST localhost:10000/run -H 'Content-Type: application/json' \
  -d '{"input_text":"复盘近期点击","intent":"review_metrics"}'
```

`intent` 可选 `word_plan_page` / `plan_promo` / `find_backlinks` / `review_metrics` / `general`；
不传则由 LLM 分类。请求还能传 `tools`（覆盖默认工具集）、`prompt_versions`
（如 `{"intent":"v2"}`）、`thread_id`。

候选池分析是另一条线，走独立入口：

```bash
curl -s -X POST localhost:10000/hotwords -H 'Content-Type: application/json' \
  -d '{"limit":20,"prompt_version":"v2"}'
```

它返回热词图自己的结果（`ok / fetched / analyzed / failed / successRate / items / traceRun`），
不套主图 state——它本就不是主图节点。

候选池分析跑完后，待开发（`saved/todo` 且带 `ai:v3:` 方案正文）的词走第三条线建页：

```bash
curl -s -X POST localhost:10000/page -H 'Content-Type: application/json' \
  -d '{}'                                  # 自动取下一条可建词，dry-run 只出预览
curl -s -X POST localhost:10000/page -H 'Content-Type: application/json' \
  -d '{"term":"when is black friday in canada","confirm":true}'
```

一次只建一页（逐词建页由调用方多次触发）。流程：取词 → 解析 8 小节方案正文成页面
结构 → 归类到已有子站（LLM 的 `target_site` 优先、站点名规则兜底）→ 渲染 HTML +
sitemap → 推 GitHub 触发 Cloudflare 自动部署（成功即落 `agent_pages` 台账）→ 线上验活。
归不进任何已有站的词报错说明要走「建新站」（尚未实现）。

候选池分析链路：直读三方共用的共享 KV（radar 写入的候选池 + console 整理的
`saved`/`rejected`/`tags`）→ 补 SERP 竞争度与 30 天趋势（读缓存，缺失现抓并写回）
→ 外部 LLM 出 8 小节站点方案 → 直写 KV（`ai:v3:` 正文 / `aihints` 信号 / `rejected`
不做 / `saved` 待开发）。实现按层归位：`graph/hotword_graph.py`（图装配 + 运行入口）→
`nodes/hotword_nodes.py`（每轮问 tool 取一词 → 分析 → 再问还有吗）→
`services/hotword_service.py`（单词分析）
→ `tools/adapters/terms/`（取数、判据、LLM、写账本叶子）。统一入口是 `src.app.graph` 包。

热词循环就是一张固定流程的图：`START → fetch_one（问 tool 要一个词）→ analyze_one →
should_continue ─┬→ fetch_one（还有词）└→ END（没有 / 达上限）`。每轮真去 KV 问一个词，
处理完再问「还有吗」，串行推进——没有快照队列、没有 Python 层的 while/for。

## 目录

```
src/app/
├── enums.py                 ★枚举唯一真源：29 个 StrEnum 族（意图/状态/类型/kind…），逐值注释含义
├── main.py / config.py      进程入口（uvicorn）/ Settings 单例 + 站群清单 + 意图兼容别名
├── api/routes.py            FastAPI /run /hotwords /page /health /llm-quota
├── graph/                   user_graph（用户线）· hotword_graph（热词循环图）· page_graph（建页子图）
│                            · state · constants(Names/Tags) · edges
├── nodes/                   intent · processing · validation · output · error · hotword_nodes（逐词）· page_nodes（建页四步）
├── services/                llm · prompt · tool · evaluation · hotword_service · page_build_service
├── tools/
│   ├── base.py              契约（BaseTool / RuntimeTool / ToolOutput）
│   ├── registry.py          门面装配（RuntimeService）+ 31 个工具注册
│   ├── *_tools.py           6 个业务域：<Domain>Ops 实现 + 工具类
│   └── adapters/            cloudflare · terms · hosting · publishing
├── models/                  ExtractionSchema · WorkflowResult
├── prompts/                 提示词唯一真源 = Radar D1（d1_source.py 读 prompts_config / prompt_defaults_config + 60s 缓存）
│   ├── d1_source.py         D1 全表快照 + 失败旧快照兜底；本地 versions/*.txt 已删除
│   └── registry.py          版本解析（缺失回退默认版本）+ 读库；test/conftest.py 注入桩
│                            D1 由 site-console /rules/prompts 页面维护，改措辞不发版
├── memory/checkpointer.py   memory / sqlite
├── llm/provider.py          OpenAI 兼容客户端
├── observability/           metrics · langsmith · trace（图轨迹）
└── utils/                   logger（单行 JSON）
config/sites.json            32 个站点的清单（配置，不是代码）
templates/site-page.html     内容页模板（11 个占位符）
migrations/0001_agent_tables.sql  D1 台账表
```

**脚手架**：本项目由 [`langgraph-init-cli`](https://pypi.org/project/langgraph-init-cli/)
**0.1.5** 的 `production` 模板生成（`langgraph-init site-agent --template production`）。
生成后删掉了模板的 `storage/`、`evaluation/`、`versioning_service.py` 与 4 个 demo 工具，
换成 D1/KV 持久化与 25 个业务工具。逐项差异和“别重复生成”的原因见
[docs/10-structure.md §1](docs/10-structure.md#1-脚手架来源)。

## 关键约束

1. 依赖单向：`main/api → graph → nodes → services → tools → adapters`；
   **tools 不依赖 services**，adapters 不依赖 graph / nodes / services，
   `tools/base.py` 只依赖标准库与 pydantic。
   函数内 import、运行入口与图装配的分层穿透都已清零：两张图在 `graph/` 下对称命名
   （`user_graph.py` / `hotword_graph.py`），统一门面是 `graph/__init__.py`；
   候选池分析走独立入口 `POST /hotwords` → `graph.run_hotwords`，不经过工具注册表。
   包内 `src.app.*` 依赖一律顶层声明（第三方 SDK 按需加载除外）。
   这些约束由 `test/test_architecture.py` 的 AST 守卫守着。
   `src.app.enums` 是零依赖纯数据模块，是唯一被放行给 base/sql 叶子引用的包内模块。
2. 门面靠构造注入：`RuntimeService` 在 `registry.py` 组装后注入 31 个工具，
   外部可通过 `tool_registry.bind(...)` 整体换门面。
3. 站点信息只在 `config/sites.json`；域名只在 `FLEET_DOMAIN`。
4. SQL 只在 `tools/adapters/cloudflare/sql.py`；表名列名只来自白名单契约。
5. **枚举值只在 `src/app/enums.py`**（29 个 `StrEnum` 族，逐值注释含义）。
   状态 / 意图 / 类型 / kind 这类「有限取值 + 业务含义」的字段一律引用枚举成员
   （写值时用 `.value` 保持 str 口径），禁止在业务代码里写裸字符串。
   `config.INTENTS`、`constants.Names/Tags`、`curation.STATUSES/HINT_NAMES` 均为枚举派生的
   兼容别名。`test/test_enums.py` 锁定取值集合与字符串兼容性，防回退成魔法值。
6. `/run` 没有鉴权，上线前必须在网关层加访问控制。

## 文档

| 文档 | 内容 |
|---|---|
| [01-overview](docs/01-overview.md) | 服务对象、一轮做什么、不做什么、风险 |
| [02-architecture](docs/02-architecture.md) | 分层与依赖、装配、运行时序、部署拓扑、边界、扩展点 |
| [03-tech-design](docs/03-tech-design.md) | 状态契约、图、节点、服务、工具、适配器、SQL、表结构 |
| [04-runbook](docs/04-runbook.md) | 本地 / 测试 / D1 建表 / Render / 排障 / 安全 |
| [05-tools](docs/05-tools.md) | 31 个工具速查、意图映射、站点清单、新增清单 |
| [10-structure](docs/10-structure.md) | 结构唯一口径（脚手架来源、分包规则、文件级目录） |
| [glossary](docs/glossary.md) | 术语表 |

## 协议

PolyForm-Noncommercial-1.0.0，署名 lvyafei。可学习、研究、修改与非商业使用，禁止商业使用。
