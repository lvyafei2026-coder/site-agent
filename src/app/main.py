# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""进程入口：`python -m src.app.main` 常驻，只承载对外用户线（HTTP）。

热词分析没有后台线程：它是独立 API `POST /hotwords`，由前端按需触发；
图本身仍是「取一词 → 分析 → 再问还有没有」的固定循环（graph/hotword_graph.py）。
本模块只拉起 uvicorn；langgraph.json 的 `run` 入口跑一轮用户线主图。
入口只从 graph 包取图——不穿透到 services / tools / adapters。
"""

from __future__ import annotations

import os
import time

import uvicorn

from src.app.api.routes import api
from src.app.config import settings
from src.app.graph import get_user_graph
from src.app.graph.state import initial_state
from src.app.observability.metrics import metrics
from src.app.observability.trace import run_traced
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PROMPT = "查看站群近期热词、新页与点击数据，给出本轮运营结论。"


# ══ 用户线唯一入口 ═════════════════════════════════════════════════════
def run() -> dict:
    """跑一轮用户线主图（意图分类→取数→校验→输出）；LangGraph 图入口（langgraph.json）。"""
    result, trace = run_traced(
        get_user_graph(),
        initial_state(DEFAULT_PROMPT),
        config={
            "configurable": {"thread_id": f"cli-{int(time.time())}"},
            "run_name": "site-agent:cli",
            "tags": ["site-agent", "cli"],
        },
    )
    logger.info("workflow_complete",
                extra={"intent": result.get("intent"), "path": trace["path"],
                       "totalMs": trace["totalMs"], "metrics": metrics.snapshot()})
    return result


def serve() -> None:
    """常驻 HTTP 服务：主线程 uvicorn 承载 /run、/hotwords、/health 等所有入口。"""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    logger.info("service_start", extra={"host": host, "port": port})
    uvicorn.run(api, host=host, port=port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    serve()
