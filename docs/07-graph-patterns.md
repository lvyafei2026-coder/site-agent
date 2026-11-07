# 07 · LangGraph 模式选型评估

> **状态：待评审，未实施。代码零改动。**
> 评估日期：2026-11-07。对照 LangGraph 官方 `workflows-agents` / `graph-api` / `interrupts` 文档，
> 并在项目锁定范围 `langgraph>=0.2.60,<0.4` 内用 `0.3.34` 做了行为实测。
> 评审通过并实施后，把结论合并进 `02-architecture.md` / `03-tech-design.md`，删除本文，避免同一主题两处留档。

---

## 1. 结论先行

**当前的图不是 ReAct，也不该改成 ReAct。** `graph.py` 是固定五节点流水线
（`sense → decide → execute → verify → learn`）+ 一处条件边 + `execute` 内的 `interrupt()`。
在官方分类里它属于 **prompt chaining（主骨架）+ 极轻的 routing + human-in-the-loop**。
这个选择是对的：场景要的是可预测、可审计、可逆、成本可控，而不是让 LLM 自己决定下一步调什么工具。

需要补的只有三处，按性价比排序：

| 优先级 | 模式 | 解决什么 | 改动量 |
|---|---|---|---|
| **P0** | 轻量 **reflection**（不改图） | `verify` 的失败原因没有回流到下一次决策，`learn` 只记账不学习 | 约 20 行，零结构风险 |
| **P1** | **orchestrator-worker（`Send`）** | 循环内 `interrupt()` 导致每次 resume 重放此前所有已批动作 | 改 `graph.py` + `state.py` + `execute.py` |
| **P2** | **routing 显式化 + subgraph** | 四类任务现在靠 `get_task()` 在节点内 switch，无法按任务挂不同的等待/校验节点 | 改 `graph.py`，节点实现不动 |
| 可选 | **evaluator-optimizer 回边** | `retry` 字段累加了但图上没有回边，`verify` 是半成品评估器 | 只给 `gen-page` 的内容生成接，且必须限次 |

明确不做：多智能体 supervisor / swarm、plan-and-execute、让 LLM 做 router、图内 sleep 自循环。理由见 §6。

---

## 2. 口径：官方的 5 + 1 分类

LangGraph 官方文档把编排分成两类：

> Workflows have predetermined code paths and are designed to operate in a certain order.
> Agents are dynamic and define their own processes and tool usage.

| 类别 | 模式 | 官方「何时用」 |
|---|---|---|
| workflow | prompt chaining | 任务可拆成更小的、可验证的步骤 |
| workflow | parallelization | 多个独立子任务可同时跑，或同一任务跑多次投票 |
| workflow | routing | 输入先分类，再交给专门流程 |
| workflow | orchestrator-worker | 子任务**无法预先定义**，由编排者动态拆分（用 `Send`） |
| workflow | evaluator-optimizer | 有明确成功标准但需要迭代才能达到 |
| agent | tool-calling loop（俗称 ReAct） | 问题和解法**都不可预测**，需要 LLM 持续反馈循环自选工具 |

官方文档里 `ReAct` 这个词其实不出现；对应的是 `Agents` 一节的 tool-calling loop
（`llm_call` ↔ `ToolNode` 的条件回环，`should_continue` 判断有没有 `tool_calls`）。
所以「除了 ReAct 还有哪些模式」在官方语境下 = **五类 workflow 里哪几类适合这个场景**。

关键 API 对照：`add_conditional_edges`（routing / 回环）、`Send`（动态 fan-out）、
`Annotated[..., operator.add]`（并行写共享 key 的 reducer）、`interrupt` + `Command(resume=...)`（HITL）、
subgraph（状态隔离）。Functional API（`@entrypoint` / `@task`）与 Graph API 共用同一个 checkpointer，
适合顺序链 + 并行 fan-out 的简单编排，但条件边、子图、interrupt 的表达力不如 Graph API——本项目已用
`StateGraph`，没有理由换。

---

## 3. 这个场景的六个约束

判据不是「哪个模式更先进」，而是下面六条。任何模式违反其中一条就不适合：

1. **流程可预测**：4 类任务（`gen-page` / `daily-report` / `backlink` / `promo`）由 cron 触发，
   `task_type` 是确定性的，处理步骤固定。
2. **动作不可逆**：建站 = git push + CF 构建；发推 = 对外发布。必须有 `interrupt()` 人审。
3. **红线是代码不是 prompt**（ADR-6）：`promo.validate()`、`gen_page.is_dead()` 是确定性校验器，
   不能被 LLM 的自由决策绕过。
4. **资源紧**：512MB / Starter $7 / LLM 走外部 API 且有熔断（ADR-1、ADR-2）。
   多一次 LLM 调用就是多一份成本和失败面。
5. **长时挂起 + 事件驱动唤醒**：见 `06-event-driven-resume.md`，等待跨天，
   `thread_id` 必须稳定，resume 由外部 HTTP 触发。
6. **可审计、可重放**：checkpointer 落 `/data/agent.sqlite`，所有动作有 `key` 幂等键。

ReAct 违反 1、3、4、6：自由循环让步骤数不可预测、工具选择可能绕过确定性校验、
LLM 调用次数不可控、循环轨迹难审计。这也是 ADR-2「只用三件」的实际含义。

---

## 4. 模式适配矩阵

| 模式 | 适配度 | 判断 |
|---|---|---|
| **prompt chaining** | ★★★ 已在用 | 现状主骨架。每步可单独测试、checkpoint 粒度自然、红线校验能插在固定位置。**保留，不要重构**。 |
| **human-in-the-loop** | ★★★ 已在用 | 严格说是能力不是模式，但是本场景的核心。当前是 approve/reject；06 方案要加 `data-ready` 唤醒；ADR-5 的推翻条件（改参数后批准）还可用 `Command(resume={...})` 传结构化值实现。 |
| **routing** | ★★ 值得显式化 | 现在只有 `decide → execute/learn` 一个二元分支，而四类任务的差异藏在 `get_task()` 里。**改成入口 router → 四类任务 subgraph**，好处是：06 方案的 `wait_hotwords` 可以只挂在 `gen-page` 分支、每类任务独立 prompt/校验/熔断、单独加指标。注意 router 必须读 `state["task_type"]` 做确定性分发，**不要用 LLM 分类**（白送一次不确定性和一次调用）。 |
| **orchestrator-worker（`Send`）** | ★★ 最该补 | `decide` 产出 N 个 action，现在 `execute` 用 for 循环串行 + 循环内 `interrupt()`。改成 `Send("exec_worker", {...})` 后：一个动作一个 worker、一个 worker 一次 `interrupt()`、单动作失败不阻塞其他、审批粒度从「整批」变「逐条」。**硬前提见 §5.2**。 |
| **parallelization** | ★ 谨慎 | 固定 fan-out（多条 `START` 边）不适合：动作数量运行时才知道。真要并行只有 `Send` 一条路，且必须限并发——512MB + 外部 LLM/部署 API，N=40 个 worker 同时跑会打爆内存和额度。建议 worker 数上限 `MAX_PAGES_PER_RUN`，或分批 `Send`。 |
| **evaluator-optimizer** | ★★ 有条件 | 适合「生成内容页」这种有明确验收标准、可以重做的环节（SEO 检查、红线检查、线上可访问）。**不适合发推/建站这类不可逆动作**：回边重跑必须保证幂等，否则重复发布。`retry` 要配合 `recursion_limit` 或显式次数上限（建议 2 次），且**只对 `decide` 之前的生成阶段回边，绝不能跨过已执行的 `execute`**。 |
| **reflection / 轻量反馈** | ★★★ 最便宜 | 不接回边也能做：把 `verify` 的失败原因写进 KV/D1，下一次 `decide` 的 `build_user_prompt()` 带上最近 N 次失败摘要。零结构风险、零额外 LLM 调用（只多几百 token），就能让「learn」名副其实。 |
| **plan-and-execute / ReWOO** | ☆ 暂不需要 | 适合「一个目标跨多步、需要根据结果动态重规划」。当前每类任务是单轮决策，planner + replanner 会多两次 LLM 调用、replan 循环难收敛、plan 状态还要持久化。**等 06 方案落地后确实出现多阶段编排再评估**，别提前上。 |
| **multi-agent supervisor / swarm** | ✗ 不要 | 四类任务的工具集和 prompt 没多到需要拆 agent；监督者本身是一次 LLM 路由调用，引入不确定性；512MB / $7 跑不动多 agent；站群运营要的是可审计确定性，不是 emergent 协作。 |
| **subgraph（状态隔离）** | ★★ 与 routing 同一件事 | 这里说的不是多智能体，而是用子图隔离每类任务的 state。共享 schema 的子图可以直接 `add_node`；父图挂 checkpointer 时子图自动继承，子图里的 `interrupt()` 在父图 `invoke` 上照样可见，`Command(resume=...)` 照样能恢复（checkpoint namespace 形如 `child:uuid`）。 |
| **图内 sleep 自循环** | ✗ 反模式 | 用 `END → START` 或 sleep 节点实现「等热词」会撞 `recursion_limit`、checkpoint 无限膨胀、崩溃无法自恢复，而且 §5.1 的节点重跑语义会让 sleep 彻底错乱。等数据必须用 `interrupt()` 挂起 + 外部 resume。 |

---

## 5. 本轮实测发现（比模式清单更重要的三件事）

### 5.1 循环内 `interrupt()` 会重放此前所有已批动作

`nodes/execute.py` 把 `interrupt()` 放在 `for action in actions` 里，且 `done` 集合来自节点返回值
`executed`。但 **LangGraph 只在节点成功 return 时才写 state**：中间挂起时节点没 return，
`executed` 不会进 checkpoint。于是每次 resume 都从头重跑该节点，把此前所有已批动作重新执行一遍。

用项目锁定的 `langgraph 0.3.34` + `InMemorySaver`，按 `execute.py` 的结构复现（3 个动作 A/B/C，
依次 resume `True / False / True`）：

```
初始挂起于: ['A']
resume1 传 True  -> 挂起于 ['B']   累计副作用 = [('A', True)]
resume2 传 False -> 挂起于 ['C']   累计副作用 = [('A', True), ('A', True), ('B', False)]
resume3 传 True  -> 挂起于 []      累计副作用 = [('A', True), ('A', True), ('B', False),
                                              ('A', True), ('B', False), ('C', True)]
最终 state.executed = [A:True, B:False, C:True]
```

读法：

- **最终语义是对的**，没有发生「批准值串位」——因为 `executed` 直到最后一次才落盘，
  `done` 全程为空，`interrupt` 的调用顺序没有左移，按索引匹配仍然正确。
- **但 A 执行了 3 次、B 执行了 2 次**。项目现在靠动作级幂等兜底：`gen_page.execute_action`
  先查 `agent_pages` 状态（已部署就 `skipped`）、`promo.execute_action` 先查排期。
  所以线上没出事故，代价是重复的部署检查 / D1 查询 / 日志 / 失败动作的熔断计数。
  **风险在新增任务**：只要哪个 `execute_action` 漏了幂等，就是真的重复建站 / 重复发推。
- 官方 `interrupts` 文档的对应规则：恢复会**重跑整个节点**；同节点多个 `interrupt`
  按**严格索引**匹配 resume 值，禁止条件跳过、重排或 `while True` 循环；`interrupt` 前的副作用必须幂等。
  并行分支的多中断要用 **interrupt ID → resume value 的 map** 一次性恢复
  （`Command(resume={id: value})`），不能靠顺序猜。

修复方向二选一：

1. **一个动作一个 `Send` worker**（推荐，对应模式级改进）：每个 worker 只有一个 `interrupt()`，
   执行完立即 return 并落 checkpoint。注意 06 方案的 `/resume` 要支持 interrupt ID map，
   或改成 console 一次只审批一条。
2. **拆节点**：`approve_gate`（收集审批）与 `apply_actions`（执行）分开，
   让挂起发生在不含副作用的节点里。改动比 `Send` 小，但仍要处理多审批的索引问题。

### 5.2 `OpsState` 没有任何 reducer，直接上 `Send` 会静默丢结果

`src/site_agent/state.py` 的 `OpsState` 全是普通 `TypedDict` 字段（`executed` / `checks` / `errors` / `retry`…）。
Graph API 默认 reducer 是 **last-write-wins**：并行 worker 同时写 `executed`，后写的覆盖先写的，
而且不报错。上 `Send` 之前必须改成下面这样（**改动提案，不是仓库现有代码**）：

```python
from typing import Annotated
import operator

class OpsState(TypedDict, total=False):
    executed: Annotated[list[dict], operator.add]
    checks:   Annotated[list[dict], operator.add]
    errors:   Annotated[list[str], operator.add]
    ...
```

`retry` 是标量且多 worker 会同时写，建议保留 last-write-wins 但由汇聚节点统一设置，
不要在每个 worker 里 `+1`。另外要注意：带合并 reducer 的字段返回空列表**不会清空**，
重试计数要跨轮清零得用 `Overwrite`。

### 5.3 `retry` 是死字段，`verify` 是半成品 evaluator

`nodes/verify.py` 在失败时 `return {"retry": state.get("retry", 0) + 1}`，
但 `graph.py` 里是 `execute → verify → learn → END`，**没有任何边消费 `retry`**。
也就是说 evaluator 只做了「打分 + 累加计数 + 触发熔断」，没有「带反馈重做」这一半。

两条低成本出路：

- **不接回边**：把 `checks` 里的失败原因写进 KV，下一次 `decide` 的 prompt 带上（轻量 reflection，P0）。
- **接回边**：只对 `gen-page` 的内容生成阶段做 `verify → decide` 的条件回边，
  `retry < 2` 才回，且回边前必须确认没有任何动作已经执行。发推/建站类动作一律不自动重试。

### 5.4 附带发现：本机测试从未编译过图

`.venv` 里原本只装了 `pytest / requests`，**没有 `langgraph`**；`pytest 7/7` 全是纯逻辑测试，
没有任何用例 import `graph.py`。本轮为实测临时装了 `langgraph 0.3.34`（未改仓库文件）。
结论：图结构、checkpointer、`interrupt` 语义目前**没有自动化回归保护**，
而 ADR-2 恰恰把「手写回归测试」列为缓解手段。建议阶段 0 至少补一个
「编译图 + 挂起 + resume」的集成用例，用 `InMemorySaver` 即可，不需要真数据库。

---

## 6. 明确不做，以及为什么

| 不做 | 理由 |
|---|---|
| 换成 ReAct / tool-calling loop | 违反约束 1/3/4/6；自由工具选择会绕过 ADR-6 的确定性校验器 |
| LLM 做 router | `task_type` 由 cron 给定，是已知量；再让 LLM 分类等于白送一次不确定性和一次调用 |
| multi-agent supervisor / swarm | 工具集规模不支撑；监督者多一次 LLM 调用；512MB / $7 跑不动；可审计性下降 |
| plan-and-execute / ReWOO（现阶段） | 单轮决策够用；planner/replanner 增加调用与收敛风险；等 06 方案真的出现多阶段编排再说 |
| 用 `END → START` / sleep 节点自循环等数据 | 撞 `recursion_limit`、checkpoint 膨胀、崩溃不可恢复、与节点重跑语义冲突 |
| 把 `verify` 回边接到已执行的动作上 | 不可逆动作自动重试 = 重复发布；幂等兜底也不能当成设计依据 |

---

## 7. 与 `06-event-driven-resume.md` 的关系

两份文档不冲突，一份讲**触发与通信**（Render web service、`/resume`、console 审批页），
一份讲**图内部结构与模式**（本文）。有两处必须一起拍板：

1. **06 方案新增的 `wait_hotwords` 节点**应该挂在 `gen-page` 的 subgraph 里，
   而不是塞进共用的 `sense`（`sense` 被 4 类任务共用，重跑时只该重跑纯读检查）。
   这个挂法依赖本文 P2 的 routing 显式化。
2. **06 方案的 `/resume` 契约**目前是「一个 thread 一个 resume 值」。
   如果采用本文 P1 的 `Send` 并行 worker，多个 pending interrupt 需要
   `Command(resume={interrupt_id: value})`，接口要同步支持 map；
   或者保持「一次只审批一条」的串行语义，用稳定 `thread_id` 逐条 resume。
   **建议先串行**（改动小、和 06 方案一致），等真的需要批量审批再上 map。

另外 06 方案 §5.1 提到 `resume.py` 要泛化 resume 值（`approved: bool` → 任意 JSON），
本文 §4 的 HITL 一行与此一致：`data-ready` / `approval` / `approval-with-params` 三类值都要能传。

---

## 8. 验收清单（实施时逐条核）

- [ ] `npm`-侧无关；`pytest` 新增「编译图 + interrupt + resume」集成用例，至少覆盖 1 个和 3 个动作
- [ ] 3 个动作依次审批时，`execute_action` 的累计调用次数 = 3（不是 6）
- [ ] `OpsState` 的并行写字段全部有 reducer；`Send` N 个 worker 后汇聚结果长度 = N
- [ ] `wait_hotwords` 只在 `gen-page` 分支上；其他三类任务不受影响
- [ ] evaluator 回边限次生效，且不会跨过已执行动作
- [ ] 06 方案的 `/resume` 与本文选定的 interrupt 语义对齐（串行 or map）
- [ ] 红线校验器仍在 LLM 之后、执行之前的确定性节点里（ADR-6 不退化）

---

## 延伸阅读

- LangGraph 官方：Workflows and agents（5 类 workflow + agent）
- LangGraph 官方：Graph API overview（`Send` / reducers / subgraph / `recursion_limit`）
- LangGraph 官方：Interrupts（节点重跑、索引匹配、并行分支的 interrupt ID map）
- 本项目：`02-architecture.md` ADR-2 / ADR-5 / ADR-6，`03-tech-design.md` §4 / §5，
  `06-event-driven-resume.md`
