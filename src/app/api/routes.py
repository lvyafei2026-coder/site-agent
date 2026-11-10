# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""HTTP 入站：POST /run 跑完一轮，GET /health 报状态与进程内计数。"""

from __future__ import annotations

import time

from fastapi import FastAPI
from pydantic import BaseModel

from src.app.config import settings
from src.app.graph.builder import get_graph
from src.app.graph.state import initial_state
from src.app.llm.quota import check_quota
from src.app.observability.langsmith import public_config as langsmith_config
from src.app.observability.metrics import metrics
from src.app.observability.trace import run_traced


class WorkflowRequest(BaseModel):
    input_text: str
    intent: str | None = None
    tools: list[str] | None = None
    prompt_versions: dict[str, str] | None = None
    thread_id: str | None = None


def invoke_workflow(payload: dict) -> dict:
    """跑完整一轮；thread_id 决定 checkpointer 的会话归属。"""
    state = initial_state(
        payload["input_text"],
        intent=payload.get("intent"),
        tools=payload.get("tools"),
        prompt_versions=payload.get("prompt_versions"),
    )
    thread_id = payload.get("thread_id") or f"run-{int(time.time() * 1000)}"
    # run_name / tags / metadata 只在 LangSmith 开启时才有意义，关闭时被忽略：
    # 有了 run_name，追踪列表里每条是 site-agent:select_pages 而不是 "LangGraph"。
    result, trace = run_traced(
        get_graph(),
        state,
        config={
            "configurable": {"thread_id": thread_id},
            "run_name": f"site-agent:{payload.get('intent') or 'auto'}",
            "tags": ["site-agent"],
            "metadata": {"thread_id": thread_id,
                         "intent": payload.get("intent") or "",
                         "input_text": payload["input_text"][:200]},
        },
    )
    # trace / langsmith 都是附加字段，不属于 WorkflowState：老前端忽略也不受影响
    return {**result, "trace": trace, "langsmith": langsmith_config()}


api = FastAPI(title=settings.project_name)


@api.post("/run")
def run(request: WorkflowRequest) -> dict:
    return invoke_workflow(request.model_dump())


@api.get("/health")
def health() -> dict:
    return {"status": "ok", "metrics": metrics.snapshot()}


@api.get("/llm-quota")
def llm_quota() -> dict:
    """LLM 剩余额度（读推理响应头）；页面上手动点击才查，每次消耗 1 次请求额度。"""
    return check_quota()
