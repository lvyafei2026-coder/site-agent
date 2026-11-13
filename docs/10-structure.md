# 10 · 结构口径

> 文件级结构的唯一口径：脚手架来源、分包规则、哪一层放什么、依赖只能怎么走、装配在哪、
> 改代码前该守的硬约束。新增或移动文件时同步本文件，其它文档不重复描述目录。

## 1. 脚手架来源

项目不是手搭的，起点是 PyPI 包 **`langgraph-init-cli` 0.1.5** 的 `production` 模板，
生成后按站群需求改造。模板本身只提供通用骨架，业务代码全部是后加的。

| 项 | 值 |
|---|---|
| 脚手架 | `langgraph-init-cli` 0.1.5（PyPI，Development Status 4 - Beta） |
| 安装 | `pip install langgraph-init-cli`（或 `pipx install langgraph-init-cli`） |
| 生成命令 | `langgraph-init site-agent --template production` |
| 模板档位 | `base` 最小图 / `advanced` 模块化 + 真实 LLM / `production` 全套 tooling + observability + evaluation |
| 生成后自检 | `cd site-agent && cp .env.example .env && pip install -e . && python -m src.app.main` |
| 本项目的两个入口 | CLI `python -m src.app.main`；HTTP `uvicorn src.app.api.routes:api`（`langgraph.json` 指向前者） |

**不要重复生成**：CLI 没有 upgrade 子命令，在已有目录重跑会覆盖同名文件。要跟新版本只能
另起临时目录生成一次再人工 diff，不要直接覆盖。

模板原样结构（生成那一刻的 `src/app/`，13 个包 + 2 个模块 = 15 个代码单元）：

```
src/app/
├── main.py            run()：样例 state + app.invoke
├── config.py          Settings dataclass（.env 全量读口）
├── api/               /run · /health
├── graph/             builder · state · edges · constants · registry(Names/Nodes/Tags)
├── nodes/             intent · processing · validation · output · error
├── services/          llm · prompt · tool · evaluation · versioning
├── tools/             BaseTool + registry + calculator / retriever / db_query / http
├── prompts/versions/  intent/v1+v2 · extraction/v1 · validation/v1
├── models/            ExtractionSchema · WorkflowResult
├── storage/           workflow_store · prompt_store · cache（进程内单例）
├── memory/            MemorySaver / SqliteSaver / PostgresSaver
├── llm/provider.py    get_llm()：OpenAI / Anthropic / Google / WatsonX
├── observability/     metrics 计数器 · @traceable 装饰器
├── evaluation/        字段覆盖率评估器 + 置信度公式
└── utils/logger.py    JSON 日志
```

### 1.1 与模板的差异

| 模板项 | 本项目 | 原因 |
|---|---|---|
| `nodes/` 节点名 `intent` / `processing` / … | `classify_intent` / `run_processing` / `validate_result` / `build_output` / `handle_error` | LangGraph 0.3.x 节点名不能与 state 字段同名，否则 `ValueError: 'intent' is already being used as a state key`；统一动词前缀 |
| `graph/registry.py`（`Names` / `Nodes` / `Tags` 三件套） | 删除；只留 `constants.py` 的 `Names` + `Tags`，节点函数在 `builder.py` 里直接 import | 一个节点一个文件已经够清楚，`Nodes` 层是多余间接 |
| `services/versioning_service.py` | 删除 | 单轮流水线不保留跨轮版本状态；prompt 版本走请求 `prompt_versions` + `prompts/registry.py` |
| `evaluation/`（独立包） | 合并成 `services/evaluation_service.py` | 只有一个评估器 + 一档阈值，独立包只让依赖图多一跳 |
| `storage/`（进程内单例） | 删除 | 模板的 `workflow_store` / `cache` 重启即丢；本项目的持久化真源是 D1（`agent_*` 7 张表）+ KV（`agent:` 前缀），不能有两份“真相” |
| `tools/` 四个 demo（calculator / retriever / db_query / http） | 换成 6 个业务域 `*_tools.py` + `adapters/`，26 个工具 | demo 没有业务含义；能力清单见 `05-tools.md` |
| `observability/` 的 `@traceable` | 真 `langsmith.traceable` + `wrap_openai`，另加 `trace.py` | 模板装饰器只打日志不上报；图轨迹要随 `/run` 返回 |
| `llm/provider.py` 四家 provider | 只留 OpenAI 兼容 + 无 key 确定性回退 | 站群只用一家兼容接口，node 里不做 SDK 分支 |
| `prompts/versions/` | 保留 `intent/v1,v2`、`extraction/v1`；删 `validation/v1`；★2026-11-13 新增 `intent_user` / `extraction_user` / `hotwords` / `hotwords_user` | 校验是确定性代码，不需要 prompt；所有发给 LLM 的文本（含原 hardcode 在 `hotwords.py` 的 `AI_SYSTEM`）统一进版本目录 |
| `config.py` | 保留并扩展 `FLEET_DOMAIN` / `SITES` / `INTENTS` | 模板只有通用 Settings |
| `models/` · `api/` · `main.py` · `memory/` · `utils/` | 保留，按需加字段 | — |

模板没有、本项目新增（业务必需）：

- `config/sites.json`（32 个站点清单）· `templates/site-page.html`（内容页模板，
  11 个占位符）· `migrations/0001_agent_tables.sql`（D1 7 张表）
- `render.yaml` · `langgraph.json` · `.env.example` 的站群字段
- `test/test_architecture.py`：把“依赖只能向下”写成可执行断言的形式

### 1.2 脚手架留下的工程文件

| 文件 | 内容 | 本项目改动 |
|---|---|---|
| `pyproject.toml` | setuptools 构建；`packages.find include=["src*"]`；`[project.scripts] site-agent = "src.app.main:run"`；`requires-python >= 3.11`；`pytest` 的 `pythonpath=["src","."]` + `testpaths=["test"]` | 版本改 `0.2.0`；依赖加 `langsmith>=0.1.140,<1`；dev 组保留 `pytest` / `httpx` |
| `.env.example` | 模板变量（`LOG_LEVEL`、`MEMORY_BACKEND`、`LLM_*`、`LANGSMITH_*`、`MAX_VALIDATION_RETRIES`…） | 扩到 46 行，补站群字段（`FLEET_DOMAIN`、`SITES_FILE`、`CF_*`、`GH_*`、`RADAR_*`、`GSC_*`、`DEVTO_*`、`RESEND_*`） |
| `langgraph.json` | `{"dependencies": ["."], "graphs": {"site-agent": "src.app.main:run"}}` | 未改；只给 LangGraph CLI 用，线上仍走 Render + FastAPI |
| `render.yaml` | Blueprint：`pip install -e .` + uvicorn + `/health`，`sync: false` 的变量在控制台填 | 新增（模板不带部署描述） |
| `migrations/0001_agent_tables.sql` | D1 `agent_*` 7 张表 | 新增 |
| `test/` | `conftest.py` + 8 个测试模块：api / graph / tools / sql / config / observability / trace / architecture | 按业务补齐；`test_architecture.py` 是新加的守卫 |

依赖版本口径以 `pyproject.toml` 为准（`langgraph >=0.2.60,<0.4`），本文不重复列版本号。

## 2. 分包规则

1. **按技术职责切，不按业务域切**。`selection / measurement / distribution / ledger /
   promotion / publishing` 六个域只体现为 `tools/` 下 6 个文件，不占顶层包。
   顶层稳定为 **11 个包 + 2 个模块（`config.py`、`main.py`）= 13 个代码单元**，
   加业务只加文件，不加包。
2. **依赖只能向下**：`api → graph → nodes → services → tools → adapters`，
   反向 import 由 `test/test_architecture.py` 拦住，不靠 code review。
3. **新东西只有一个落点**：
   - 新外部系统 → `tools/adapters/<域>/`，并在对应 `*_tools.py` 的 Ops 里接一层
   - 新业务能力 → 对应 `*_tools.py` 加一个 `RuntimeTool` 子类 + `TOOL_CLASSES` 加一行
   - 新图步骤 → `nodes/` 一个文件 + `constants.Names` + `builder.py` 一条边（名字带动词前缀）
   - 新配置 → `config.py` 加字段；值来自 env 或 `config/sites.json`，模块里不写字面量

各代码单元的职责边界：

| 单元 | 一句话职责 | 只有它能做的事 | 与模板 |
|---|---|---|---|
| `api/routes.py` | HTTP 边界 | 解析请求体、拼 `initial_state`、返回 `{state, trace}` | 保留改造 |
| `graph/` | 图装配 | 定义 state 形状、节点顺序、条件边、提供进程级单例 | 保留，去 registry |
| `nodes/` | 单步逻辑 | 决定读/写 state 的哪些字段、失败怎么记进 `messages` | 保留改名 |
| `services/` | 无 IO 的业务判断 | 意图分类、字段抽取、意图→工具集、覆盖率评分 | 保留 4 个 |
| `tools/*_tools.py` | 26 个能力的实现 | Pydantic 参数校验、转调哪个 Ops 方法、`kind` 标注 | 全换 |
| `tools/adapters/` | 唯一外部 IO 层 | HTTP / SQL / git / SMTP；超时、重试、错误归一成 `ok=false` | 新增 |
| `tools/base.py` | 契约叶子 | `BaseTool` / `RuntimeTool` / `ToolOutput` / `stable_key`，不 import 任何 `src.app.*` | 保留 |
| `tools/registry.py` | 装配 | `RuntimeService` 组合 6 个域 Ops + `ToolRegistry.bind()` | 保留 |
| `models/` | 跨层数据契约 | `ExtractionSchema`、`WorkflowResult` | 保留 |
| `prompts/` | **所有 LLM 文本的唯一存放处** | `registry.py` 版本解析（缺失回退 v1）、`__init__.py` 的纯加载函数供工具层用、`versions/<task>/vN.txt` | 保留 + 扩充 6 个 task |
| `llm/provider.py` | 模型客户端 | OpenAI 兼容构造、无 key 返回 `None`、`wrap_openai` | 改造 |
| `memory/` | checkpointer | memory / sqlite 选择与连接参数 | 改造 |
| `observability/` | 观测 | metrics、LangSmith 开关、图轨迹采集 | 保留 + 新增 |
| `utils/logger.py` | 日志 | 单行 JSON、`extra={"span": ...}` | 保留 |
| `config.py` | 配置唯一入口 | `Settings`、`INTENTS`、`FLEET_DOMAIN`、`SITES` | 扩展 |

## 3. 目录树

```
site-agent/
├── src/app/
│   ├── main.py                  CLI 入口：get_graph().invoke(initial_state(...))
│   ├── config.py                Settings(settings) + INTENTS + FLEET_DOMAIN + SiteConfig/SITES
│   ├── api/routes.py            FastAPI：POST /run、GET /health
│   ├── graph/
│   │   ├── builder.py           StateGraph 组装 + get_graph() 进程级单例
│   │   ├── state.py             WorkflowState（输入 / 中间 / 输出三组）+ initial_state()
│   │   ├── constants.py         Names（节点名）+ Tags（路由标签）
│   │   └── edges.py             route_after_validation + EDGE_MAP
│   ├── nodes/                   classify_intent · run_processing · validate_result · build_output · handle_error
│   ├── services/
│   │   ├── llm_service.py       意图分类 + 字段抽取（含无 key 回退）
│   │   ├── prompt_service.py    prompt 加载（版本可指定、缺失回退 v1）
│   │   ├── tool_service.py      意图 → 工具集（INTENT_TOOLS / TOOL_ARGS）
│   │   └── evaluation_service.py 覆盖率与综合分（阈值是同文件常量）
│   ├── tools/
│   │   ├── base.py              契约：BaseTool / RuntimeTool / ToolOutput / stable_key
│   │   ├── registry.py          装配：RuntimeService + TOOL_CLASSES + ToolRegistry
│   │   ├── selection_tools.py   SelectionOps + 1 个工具
│   │   ├── measurement_tools.py MeasurementOps + 2 个工具
│   │   ├── distribution_tools.py DistributionOps + 4 个工具
│   │   ├── ledger_tools.py      LedgerOps + 9 个工具
│   │   ├── promotion_tools.py   PromotionOps + 2 个工具
│   │   ├── publishing_tools.py  PublishingOps + 7 个工具
│   │   └── adapters/
│   │       ├── cloudflare/      client · database · keyvalue · sql
│   │       ├── sources/         radar · gsc · reddit
│   │       ├── hosting/         repository · renderer · verification
│   │       └── publishing/      devto · email
│   ├── models/                  ExtractionSchema · WorkflowResult
│   ├── prompts/                 registry.py + __init__.py（纯加载）+ versions/<task>/vN.txt
│   │                            intent · intent_user · extraction · extraction_user
│   │                            hotwords（逐词 system）· hotwords_user（逐词 human）
│   ├── memory/checkpointer.py   memory / sqlite
│   ├── llm/provider.py          OpenAI 兼容客户端
│   ├── observability/           metrics · langsmith(@traceable) · trace(图轨迹)
│   └── utils/logger.py          单行 JSON 日志
├── config/sites.json            32 个站点（配置，不是代码）
├── templates/site-page.html     内容页模板（11 个占位符）
├── migrations/0001_agent_tables.sql  D1 台账表
└── test/                        api · graph · tools · sql · config · observability · trace · architecture
```

分包按**技术职责**（入口 / 编排 / 步骤 / 服务 / 工具 / 适配器），业务域只在
`tools/*_tools.py` 内部体现。这样加一个能力只需要动一个域文件 + 一行注册。

## 4. 依赖方向

```mermaid
flowchart LR
  api --> graph --> nodes --> services --> tools --> adapters
  services --> prompts
  tools --> prompts
  adapters --> prompts
  services --> observability
  services --> llm
  nodes --> models
  graph --> memory
  config -.-> api
  config -.-> graph
  config -.-> nodes
  config -.-> services
  config -.-> tools
```

规则只有一条：**services 可以依赖 tools，tools 不能依赖 services**。
`prompts/` 是基础设施，`services` 与 `tools/adapters` 都能直接依赖它——所以热词逐词分析
从 `prompts/__init__.py` 取纯加载函数，而不是绕道 `services/prompt_service.py`。

工具包内还有两条更细的：

1. `tools/base.py` 是纯叶子：只依赖标准库与 pydantic，不 import 任何 `src.app.*`。
2. 工具层没有函数内 `import`：顺序由模块层级保证（`registry → *_tools → base`）。
   `adapters/` 例外，那里允许为可选依赖做延迟导入。

## 5. 装配与工具契约

外部 IO 的门面是 `RuntimeService`（6 个域 Ops 的多继承组合，本身没有实现），
装配与注入都在 `registry.py`：

```python
class RuntimeService(SelectionOps, DistributionOps, LedgerOps,
                     MeasurementOps, PromotionOps, PublishingOps):
    """唯一门面：实现见各域 Ops，这里只有组合。"""

class ToolRegistry:
    def bind(self, ops):        # 生产绑 RuntimeService，测试绑 FakeRuntime
        self.ops = ops
        self._tools = {t.name: t for t in (cls(ops) for cls in TOOL_CLASSES)}

tool_registry = ToolRegistry()
```

工具类的固定写法（26 个一致）：

```python
class GetHotwordsTool(RuntimeTool):
    """作用说明（= description，工具列表里看到的就是这行）。"""

    tool_name = "get_hotwords"
    method = "hotwords"        # 转调 self.ops.hotwords(...)
    # kind 缺省 read；ledger / write 才显式写

    class Input(BaseModel):    # 参数契约
        limit: int = Field(40, description="最多返回几条")
```

`fixed` 只在需要固定参数时写（如 `{"dimensions": ("page",)}`）；嵌套 `Input`
本身就是类属性，不需要 `Input = Input`。

## 6. 硬约束与已知坑

1. **节点名不能与 state 字段同名**：LangGraph 0.3.x 的 `add_node("intent", ...)` 与
   `WorkflowState.intent` 冲突，抛 `ValueError: 'intent' is already being used as a
   state key`。所以 `Names` 全部带动词前缀。
2. **`logger` 的 `extra` 不能用 LogRecord 保留字段**：`extra={"name": ...}` 在
   Python 3.13 抛 `KeyError: Attempt to overwrite 'name' in LogRecord`，追踪里改用
   `extra={"span": ...}`。
3. **`SqliteSaver.from_conn_string()` 返回上下文管理器**，不是 saver；直接传给
   `compile(checkpointer=...)` 会在首次写入崩溃。`memory/checkpointer.py` 自行
   `sqlite3.connect(path, check_same_thread=False)` 再构造。
4. **sitemap 不新建**：`render_files` 只在 `<asset_dir>/sitemap.xml` 已存在时插入
   `<url>`；站点仓库缺这个文件时要先补一个空 `<urlset>`。
5. **`ensure_repo` 会重置本地副本**：已存在的仓库执行 `fetch` + `reset --hard`，
   手工改动会丢，调试请另起目录。

## 7. 自查

```bash
pytest -q                                        # 56 passed
python -m pyflakes src test                      # 无输出
python -m compileall -q src test
python -m src.app.main                           # 无凭据也跑完：工具失败 → retry → error
python -c "from src.app.tools.registry import tool_registry; print(len(tool_registry.names()))"  # 25
grep -rn "set_runtime\|build_runtime\|tools/runtime" src/ test/   # 应为空
find src test -type d -name __pycache__          # 收尾清空
```
