# 10 · 结构口径

> 文件级结构的唯一口径：哪一层放什么、依赖只能怎么走、装配在哪、改代码前该守的硬约束。
> 新增或移动文件时同步本文件，其它文档不重复描述目录。

## 1. 目录树

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
│   ├── nodes/                   intent · processing · validation · output · error
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
│   ├── prompts/                 registry.py + versions/<task>/vN.txt
│   ├── memory/checkpointer.py   memory / sqlite
│   ├── llm/provider.py          OpenAI 兼容客户端
│   ├── observability/           metrics · langsmith(@traceable)
│   └── utils/logger.py          单行 JSON 日志
├── config/sites.json            32 个站点（配置，不是代码）
├── templates/site-page.html     内容页模板（11 个占位符）
├── migrations/0001_agent_tables.sql  D1 台账表
└── test/                        api · graph · tools · sql · config · architecture
```

分包按**技术职责**（入口 / 编排 / 步骤 / 服务 / 工具 / 适配器），业务域只在
`tools/*_tools.py` 内部体现。这样加一个能力只需要动一个域文件 + 一行注册。

## 2. 依赖方向

```mermaid
flowchart LR
  api --> graph --> nodes --> services --> tools --> adapters
  services --> prompts
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

工具包内还有两条更细的：

1. `tools/base.py` 是纯叶子：只依赖标准库与 pydantic，不 import 任何 `src.app.*`。
2. 工具层没有函数内 `import`：顺序由模块层级保证（`registry → *_tools → base`）。
   `adapters/` 例外，那里允许为可选依赖做延迟导入。

## 3. 装配与工具契约

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

工具类的固定写法（25 个一致）：

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

## 4. 硬约束与已知坑

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

## 5. 自查

```bash
pytest -q                                        # 47 passed
python -m pyflakes src test                      # 无输出
python -m compileall -q src test
python -m src.app.main                           # 无凭据也跑完：工具失败 → retry → error
python -c "from src.app.tools.registry import tool_registry; print(len(tool_registry.names()))"  # 25
grep -rn "set_runtime\|build_runtime\|tools/runtime" src/ test/   # 应为空
find src test -type d -name __pycache__          # 收尾清空
```
