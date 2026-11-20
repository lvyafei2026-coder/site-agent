# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""进程入口：`python -m src.app.main` 常驻，两条线在同一进程同时启动、共用底层能力。

线 1｜对外用户输入（用户线）  run() / HTTP /run
    get_user_graph()（graph/user_graph.py 主图）：classify_intent → run_processing → validate_result → output
    主线程 uvicorn 承载 api/routes.py；run() 亦作 LangGraph 图入口（langgraph.json）。

线 2｜后台自动运行（后台线）  run_background() / _background_loop()
    run_batch()（graph/hotword_batch.py 批次驱动）：invoke graph/hotword_graph.py 的循环图
    fetch_one → analyze_one → should_continue 回环。守护线程按 BACKGROUND_INTERVAL_SEC 周期驱动。

serve() 同时拉起两条线；单跑调试直接调用 run() / run_background()。两图并列、互不嵌套。
入口只从 graph 包取图与驱动批次——不穿透到 services / tools / adapters。
"""

from __future__ import annotations

import os
import threading
import time

import uvicorn

from src.app.api.routes import api
from src.app.config import settings
from src.app.graph import get_user_graph, run_batch
from src.app.graph.state import initial_state
from src.app.observability.metrics import metrics
from src.app.observability.trace import run_traced
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PROMPT = "查看站群近期热词、新页与点击数据，给出本轮运营结论。"


# ══ 线 1：对外用户输入 ═══════════════════════════════════════════════
def run() -> dict:
    """用户线：跑一轮主图（意图分类→取数→校验→输出）。

    LangGraph 图入口（langgraph.json），也可直接调用单跑调试。
    """
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


# ══ 线 2：后台自动运行 ═══════════════════════════════════════════════
def run_background() -> dict:
    """后台线：跑一批热词分析，逐词处理候选池里未跟进的热词。

    直接调 graph 的批次驱动 run_batch（invoke 后台循环图）；返回结构与 analyze_hotwords 一致。
    """
    out = run_batch()
    logger.info("background_complete",
                extra={"analyzed": out.get("analyzed"), "failed": out.get("failed"),
                       "ok": out.get("ok"), "metrics": metrics.snapshot()})
    return out


def _background_loop(stop: threading.Event, interval_sec: int) -> None:
    """后台定时循环：启动即跑一轮，之后每 interval_sec 一轮，stop 置位可中断等待。

    单条守护线程内串行执行——一轮未跑完不会叠下一轮；异常留日志后继续，保证循环
    不因单次 LLM / KV 故障停摆。
    """
    while not stop.is_set():
        try:
            run_background()
        except Exception:
            logger.exception("background_cycle_failed")
        if stop.wait(interval_sec):  # 可中断 sleep，关闭进程时立即醒
            break


# ══ 常驻：两条线同时启动 ══════════════════════════════════════════════
def serve() -> None:
    """主线程 uvicorn（用户线 /run）+ 守护线程定时循环（后台线），同一进程常驻。

    port 取 PORT，后台周期取 settings.background_interval_sec（<=0 时只跑用户线）。
    uvicorn 退出（SIGTERM / 异常）时置位 stop 并 join 后台线程，避免残留任务写半个批次。
    """
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    interval = settings.background_interval_sec

    stop = threading.Event()
    bg_thread = None
    if interval > 0:
        bg_thread = threading.Thread(target=_background_loop, args=(stop, interval),
                                     name="background-line", daemon=True)
        bg_thread.start()
    logger.info("service_start", extra={"host": host, "port": port,
                                        "backgroundIntervalSec": interval,
                                        "backgroundEnabled": bg_thread is not None})
    try:
        uvicorn.run(api, host=host, port=port,
                    log_level=settings.log_level.lower())
    finally:
        stop.set()
        if bg_thread is not None:
            bg_thread.join(timeout=5)


if __name__ == "__main__":
    serve()
