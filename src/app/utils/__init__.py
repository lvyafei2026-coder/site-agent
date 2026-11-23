# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""横切小工具：与业务无关、任何一条线都可能用的实现层模块。

判定口径一句话：**换个项目也照抄得走的，才放这里**。带业务语义的（表名、结论枚举、
提示词拼装、步骤名）留在自己的域里，哪怕它读起来很像工具函数。

    logger.py         进程内单行 JSON 日志（Render / Cloudflare 按字段检索）
    llm_io.py         一次非流式补全调用：思考开关 + 空正文现场诊断
    kv_shape.py       KV 键形状兜底读取（期望数组/对象，读歪了当空并 warn）
    source_errors.py  外部数据源整体不可用的异常契约

与 src/app/llm/ 的分工：那包是 LLM 底座（provider 建连接、completion 补全与降级、
quota 额度用量），本包的 llm_io 只做「一次调用怎么发出去、正文为空到底为什么」。
两边互引不成环：completion 复用 llm_io.thinking_kwargs，llm_io 只用 provider 的客户端。

守卫：test_architecture.py::test_utils_do_not_depend_on_business_layers——
这里不许 import graph / nodes / services / tools / adapters / api。
"""
