# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""HTTP 入站：每个场景一个入口，入口之间不互相分流。

    POST /run        常规线主图（意图分类 → 取数 → 校验 → 输出）
    POST /hotwords   热词循环图（逐词取词 → 分析 → 汇总）
    GET  /health     状态与进程内计数
    GET  /llm-quota  DeepSeek 余额与用量

入口层只做 HTTP 绑定：请求模型 → graph 层的 run_*()。不构造 state、不驱动图、
不拼 trace——两条线的构造与触发机制在 graph/ 里完全一致（normal_graph / hotword_graph），
差别只在节点类型与 state 内容。热词分析不是主图的一个节点，也不经过工具注册表。
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from src.app.config import settings
from src.app.graph import run_hotwords, run_workflow
from src.app.llm.quota import check_quota
from src.app.observability.metrics import metrics


class WorkflowRequest(BaseModel):
    input_text: str
    intent: str | None = None
    tools: list[str] | None = None
    prompt_versions: dict[str, str] | None = None
    thread_id: str | None = None


class HotwordsRequest(BaseModel):
    limit: int = 20
    prompt_version: str | None = None


api = FastAPI(title=settings.project_name)


@api.post("/run")
def run(request: WorkflowRequest) -> dict:
    """常规线主图：请求字段与 run_workflow 签名一一对应，直接展开。"""
    return run_workflow(**request.model_dump())


@api.post("/hotwords")
def hotwords(request: HotwordsRequest) -> dict:
    """热词循环图：请求字段与 run_hotwords 签名一一对应，直接展开。

    返回图终态的汇总（ok / analyzed / failed / items …）+ trace + langsmith，
    与 /run 同一套封装；limit 最大 50，由 state 工厂收口。
    """
    return run_hotwords(**request.model_dump())


@api.get("/health")
def health() -> dict:
    return {"status": "ok", "metrics": metrics.snapshot()}


@api.get("/llm-quota")
def llm_quota() -> dict:
    """DeepSeek 余额与用量：官方 /user/balance 零消耗查询 + 可选的平台用量面板。

    配了 DEEPSEEK_PLATFORM_TOKEN 才查近 30 天消费 / 请求次数 / Tokens / 累计消费；
    未配时 usage.ok=False 并给出提示，余额照常返回。只在页面手动点击时触发。
    """
    return check_quota()
