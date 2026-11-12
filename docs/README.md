# 文档索引

本目录只描述**当前实现**。不保留历史设计文档：任何被替换的方案都在代码与
`git log` 里，不在文档里重复叙述。

| 文档 | 回答什么问题 |
|---|---|
| [01-overview.md](01-overview.md) | 这个 Agent 服务谁、一轮做什么、明确不做什么、有哪些风险 |
| [02-architecture.md](02-architecture.md) | 分几层、依赖为什么单向、谁负责装配、一次请求怎么走、和站群其它系统怎么划界 |
| [03-tech-design.md](03-tech-design.md) | 状态契约、图与节点、四个服务、工具契约、适配器、SQL、D1 表、checkpointer、可观测性 |
| [04-runbook.md](04-runbook.md) | 本地跑、环境变量、D1 建表、Render 部署、验证清单、排障、安全 |
| [05-tools.md](05-tools.md) | 26 个工具速查、意图到工具集、站点清单字段、新增能力检查清单 |
| [10-structure.md](10-structure.md) | 结构唯一口径：脚手架来源与逐项差异、分包规则、文件级目录树、依赖方向、硬约束、自查清单 |
| [glossary.md](glossary.md) | 站群、编排、数据三类术语 |

## 单一真源

| 想知道 | 看这里（唯一） |
|---|---|
| 有哪些文件、放哪层 | `10-structure.md` |
| 项目从哪个脚手架生成、改了什么 | `10-structure.md §1` |
| 有哪些工具、什么参数 | `05-tools.md` |
| 有哪些环境变量 | `04-runbook.md §2` 与 `.env.example` |
| 站点怎么配 | `config/sites.json` + `05-tools.md §3` |
| 有哪些表、什么列 | `migrations/0001_agent_tables.sql` + `03-tech-design.md §8` |
| 阈值与权重 | `services/evaluation_service.py` 顶部常量 |
| 依赖方向 | `test/test_architecture.py`（可执行的规则） |

文档与代码冲突时以代码为准，并顺手把文档改回来。
