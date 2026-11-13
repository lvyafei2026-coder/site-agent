# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""HTTP 入站：POST /run 跑完一轮，GET /health 报状态与进程内计数，
POST /backfill 补写「正文已落库、状态表丢了」的热词（运维入口）。"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from src.app.config import BACKFILL_TOKEN, settings
from src.app.graph.builder import get_graph
from src.app.graph.state import initial_state
from src.app.llm.quota import check_quota
from src.app.observability.langsmith import public_config as langsmith_config
from src.app.observability.metrics import metrics
from src.app.observability.trace import run_traced
from src.app.tools.adapters.sources.hotwords import backfill_states


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


class BackfillRequest(BaseModel):
    dry_run: bool = True      # 默认只出报告；显式传 false 才真正写 KV
    limit: int = 200


@api.post("/backfill")
def backfill(req: BackfillRequest,
             x_backfill_token: Optional[str] = Header(default=None)) -> dict:
    """状态回填：扫描 ai:v3: 正文，补写丢失的 saved / rejected / aihints。

    典型用法：
      curl -X POST .../backfill -H 'Content-Type: application/json' -d '{}'
        → dry run，看哪些词会被补、哪些 unresolved
      curl -X POST .../backfill -H 'X-Backfill-Token: <token>' \
           -d '{"dry_run": false, "limit": 200}'
        → 真正补写。BACKFILL_TOKEN 配了才校验请求头；线上必须配。
    """
    if BACKFILL_TOKEN and x_backfill_token != BACKFILL_TOKEN:
        raise HTTPException(status_code=401, detail="invalid backfill token")
    return backfill_states(dry_run=req.dry_run, limit=req.limit)
