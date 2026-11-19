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

26 个工具全部是 `BaseTool` 子类，配 Pydantic `Input`，统一返回 `ToolOutput`：
13 个 `read`、10 个 `ledger`、3 个 `write`。所有外部 IO 只发生在 `tools/adapters/`。

| 记住这一条 | 含义 |
|---|---|
| **只读默认** | 默认编排一般只调 `read` 工具；`write`（`git_push`、`publish_devto_article`）必须由请求 `tools` 显式列出 |
| **一个例外：`analyze_hotwords`** | 「热词分析」意图的默认工具集就是 `analyze_hotwords`（write），语义本身就是「分析并回写候选状态」。★上线前必须加访问控制 |
| **写动作靠显式声明** | 不保存待审状态；除上述意图外，`write` 工具必须由请求 `tools` 列出，调度交给外部 cron |
| **无 LLM 也能跑** | 没有 `LLM_API_KEY` 时意图回退 `general`、抽取走本地正则，图照样跑完；热词分析拿不到 LLM 正文会跳过该词、不编造结论 |
| **工具失败不抛异常** | 单个数据源挂了只记 `ok=false`，由校验环节决定重试还是报错 |

## 快速开始

```bash
pip install -e .
cp .env.example .env
python -m src.app.main                 # CLI 跑一轮
uvicorn src.app.api.routes:api --reload --port 10000
pytest -q                              # 全量单测（含 test_hotwords.py）
```

```bash
curl -s -X POST localhost:10000/run -H 'Content-Type: application/json' \
  -d '{"input_text":"复盘近期点击","intent":"review_metrics"}'
```

`intent` 可选 `select_pages` / `plan_promo` / `find_backlinks` / `review_metrics` /
**`analyze_hotwords`（热词分析，每批 20 条未跟进词，会写状态）** / `general`；
不传则由 LLM 分类。请求还能传 `tools`（覆盖默认工具集）、`prompt_versions`
（如 `{"intent":"v2"}`）、`thread_id`。

热词分析链路：console `/api/hotwords` 取词 → radar `/api/serp` + `/api/trend` 补竞争度与
30 天趋势 → 外部 LLM 出 8 小节站点方案 → `/api/hotwords` 写回 `ai:v3:` 正文、`aihints`、
`rejected`（不做）或 `saved.status=todo`（待开发）。实现与口径见
`src/app/tools/adapters/terms/hotwords.py`（门面 → hotword_fetch/worker/batch 三层）。

## 目录

```
src/app/
├── enums.py                 ★枚举唯一真源：29 个 StrEnum 族（意图/状态/类型/kind…），逐值注释含义
├── main.py / config.py      CLI 入口 / Settings 单例 + 站群清单 + 意图兼容别名（派生自 enums.Intent）
├── api/routes.py            FastAPI /run /health
├── graph/                   builder · state · constants(Names/Tags=枚举别名) · edges
├── nodes/                   intent · processing · validation · output · error
├── services/                llm · prompt · tool · evaluation
├── tools/
│   ├── base.py              契约（BaseTool / RuntimeTool / ToolOutput）
│   ├── registry.py          门面装配（RuntimeService）+ 26 个工具注册
│   ├── *_tools.py           6 个业务域：<Domain>Ops 实现 + 工具类
│   └── adapters/            cloudflare · terms · hosting · publishing
├── models/                  ExtractionSchema · WorkflowResult
├── prompts/                 提示词唯一真源 = radar-rules D1（d1_source.py 读库+60s 缓存）
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
换成 D1/KV 持久化与 26 个业务工具。逐项差异和“别重复生成”的原因见
[docs/10-structure.md §1](docs/10-structure.md#1-脚手架来源)。

## 关键约束

1. 依赖单向：`api → graph → nodes → services → tools → adapters`；
   **tools 不依赖 services**，`tools/base.py` 只依赖标准库与 pydantic，
   工具层没有函数内 import。三条都由 `test/test_architecture.py` 守着。
   `src.app.enums` 是零依赖纯数据模块，是唯一被放行给 base/sql 叶子引用的包内模块。
2. 门面靠构造注入：`RuntimeService` 在 `registry.py` 组装后注入 26 个工具，
   测试用 `tool_registry.bind(FakeRuntime())` 整体替换。
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
| [05-tools](docs/05-tools.md) | 26 个工具速查、意图映射、站点清单、新增清单 |
| [10-structure](docs/10-structure.md) | 结构唯一口径（脚手架来源、分包规则、文件级目录） |
| [glossary](docs/glossary.md) | 术语表 |

## 协议

PolyForm-Noncommercial-1.0.0，署名 lvyafei。可学习、研究、修改与非商业使用，禁止商业使用。
