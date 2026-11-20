# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""HTTP 入站：POST /run 跑完一轮，GET /health 报状态与进程内计数，
POST /backfill 补写「正文已落库、状态表丢了」的热词（运维入口）。

分层：入口只依赖 graph 包——取图、驱动批次、运维回填都从 graph 走，不穿透到
services / tools / adapters。用户线的 analyze_hotwords 意图在入口分流到 graph 的
批次驱动（它是编排不是工具），返回体包装成与其余意图一致的 state 结构，前端零感知。
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from src.app.config import BACKFILL_TOKEN, settings
from src.app.enums import Intent
from src.app.graph import get_user_graph, run_backfill, run_hotwords
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


def _hotwords_state(payload: dict) -> dict:
    """用户线热词分析：入口分流到 graph 批次驱动，包装成与其余意图一致的 /run 返回体。

    为什么不在主图里用工具触发：批量分析是编排（invoke 循环图），属 graph 层；
    放在 tools 层会形成 tools → graph 反向依赖。入口分流后工具层只剩纯 IO，依赖单向。
    返回字段与主图一致（intent / confidence / validation / output / tool_results /
    langsmith）；批次自己的 traceRun 在 out 里供 LangSmith 回链，前端无需区分走了哪条线。
    """
    versions = payload.get("prompt_versions") or {}
    out = run_hotwords(prompt_version=versions.get("hotwords"))
    ok = bool(out.get("ok"))
    reason = out.get("error") or out.get("note") or "本批热词分析完成"
    output = (f"intent=analyze_hotwords; tools=1; analyzed={out.get('analyzed', 0)}; "
              f"failed={out.get('failed', 0)}; ok={ok}")
    # 热词图自己的 traceRun{runId,url} 已在 out 里，前端可直接回链 LangSmith；
    # 这里不伪造主图的节点轨迹——这条意图走的是后台批次线，没有主图的 5 个节点。
    return {
        "intent": Intent.ANALYZE_HOTWORDS.value,
        "confidence": 1.0,
        "validation": {"is_valid": ok, "reason": reason,
                       "tool_failures": out.get("failed", 0)},
        "output": output,
        "tool_results": {
            "analyze_hotwords": {"tool": "analyze_hotwords", "ok": ok,
                                 "result": out, "error": out.get("error", "")},
        },
        "langsmith": langsmith_config(),
    }


def invoke_workflow(payload: dict) -> dict:
    """跑完整一轮；thread_id 决定 checkpointer 的会话归属。"""
    if payload.get("intent") == Intent.ANALYZE_HOTWORDS.value:
        return _hotwords_state(payload)

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
        get_user_graph(),
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
    """DeepSeek 余额与用量：官方 /user/balance 零消耗查询 + 可选的平台用量面板。

    配了 DEEPSEEK_PLATFORM_TOKEN 才查近 30 天消费 / 请求次数 / Tokens / 累计消费；
    未配时 usage.ok=False 并给出提示，余额照常返回。只在页面手动点击时触发。
    """
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
    return run_backfill(dry_run=req.dry_run, limit=req.limit)
