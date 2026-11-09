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
from src.app.observability.metrics import metrics


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
    return get_graph().invoke(
        state, config={"configurable": {"thread_id": thread_id}})


api = FastAPI(title=settings.project_name)


@api.post("/run")
def run(request: WorkflowRequest) -> dict:
    return invoke_workflow(request.model_dump())


@api.get("/health")
def health() -> dict:
    return {"status": "ok", "metrics": metrics.snapshot()}
