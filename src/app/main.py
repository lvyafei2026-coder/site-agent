# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""进程入口：默认常驻，两条线在同一进程里同时启动、共用底层工具/适配器，图结构各管各的。

线 1｜对外用户输入（用户线）  run() / HTTP /run
    get_graph()（builder.py 主图）：classify_intent → run_processing → validate_result → output
    主线程 uvicorn 承载 api/routes.py；按 thread_id 续跑。run() 亦作 CLI 单跑 / LangGraph 入口。

线 2｜后台自动运行（后台线）  run_background() / _background_loop()
    get_hotword_graph()（hotword_graph.py 循环图）：fetch_one → analyze_one → should_continue 回环
    守护线程按 BACKGROUND_INTERVAL_SEC 周期驱动，启动即跑一轮；读一词→分析→回环直到词用完或达 limit。

serve() 同时拉起两条线；user / background 子命令保留单跑调试。两图在装配层并列、互不嵌套。

用法：
    python -m src.app.main               # 常驻双服务（默认）：HTTP 用户线 + 定时后台线
    python -m src.app.main serve         # 同上，显式
    python -m src.app.main user          # 用户线单跑一轮（原 run）
    python -m src.app.main background    # 后台线单跑一批（原 run_background）
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Optional

import uvicorn

from src.app.api.routes import api
from src.app.config import settings
from src.app.graph.builder import get_graph
from src.app.graph.state import initial_state
from src.app.observability.metrics import metrics
from src.app.observability.trace import run_traced
from src.app.tools.adapters.terms import hotwords
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PROMPT = "查看站群近期热词、新页与点击数据，给出本轮运营结论。"


# ══ 线 1：对外用户输入 ═══════════════════════════════════════════════
def run() -> dict:
    """用户线单跑一轮主图（意图分类→取数→校验→输出）。

    既是 CLI `user` 子命令，也是 langgraph.json / pyproject scripts 的图入口。
    """
    result, trace = run_traced(
        get_graph(),
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
def run_background(limit: int = 20,
                   prompt_version: Optional[str] = None) -> dict[str, Any]:
    """后台线单跑一批：驱动热词循环图，逐词分析候选池里未跟进的热词。

    走 hotwords.run_batch 复用批次 trace 外壳与结果汇总；其执行体顶层静态取
    hotword_graph.get_hotword_graph()（与用户线主图并列、互不嵌套）。返回结构与
    analyze_hotwords 一致。
    """
    out = hotwords.run_batch(limit=limit, prompt_version=prompt_version)
    logger.info("background_complete",
                extra={"analyzed": out.get("analyzed"), "failed": out.get("failed"),
                       "ok": out.get("ok"), "metrics": metrics.snapshot()})
    return out


def _background_loop(stop: threading.Event, interval_sec: int) -> None:
    """后台定时循环：启动即跑一轮，之后每 interval_sec 一轮，stop 置位可中断等待。

    单条守护线程内串行执行——一轮未跑完不会叠下一轮；异常吞掉并留日志，保证循环
    不因单次 LLM / KV 故障停摆。interval_sec <= 0 时不启动（见 serve）。
    """
    while not stop.is_set():
        try:
            run_background()
        except Exception:
            logger.exception("background_cycle_failed")
        if stop.wait(interval_sec):  # 可中断 sleep，关闭进程时立即醒
            break


# ══ 常驻双服务 ════════════════════════════════════════════════════════
def serve(host: Optional[str] = None, port: Optional[int] = None,
          interval_sec: Optional[int] = None) -> dict[str, Any]:
    """同一进程同时启动两条线：主线程 uvicorn（用户线）+ 守护线程定时循环（后台线）。

    BACKGROUND_INTERVAL_SEC <= 0 时只跑用户线（后台自动关闭，仍可 `background` 单跑）。
    uvicorn 退出（SIGTERM / 异常）时置位 stop 并 join 后台线程，避免残留任务写半个批次。
    """
    host = host or os.getenv("HOST", "0.0.0.0")
    port = int(port or os.getenv("PORT", "8000"))
    interval = int(interval_sec if interval_sec is not None
                   else settings.background_interval_sec)

    stop = threading.Event()
    started_bg = interval > 0
    bg_thread = None
    if started_bg:
        bg_thread = threading.Thread(target=_background_loop, args=(stop, interval),
                                     name="background-line", daemon=True)
        bg_thread.start()
    logger.info("service_start", extra={"host": host, "port": port,
                                        "backgroundIntervalSec": interval,
                                        "backgroundEnabled": started_bg})
    try:
        uvicorn.run(api, host=host, port=port,
                    log_level=settings.log_level.lower())
    finally:
        stop.set()
        if bg_thread is not None:
            bg_thread.join(timeout=5)
    return {"host": host, "port": port, "backgroundIntervalSec": interval,
            "backgroundEnabled": started_bg}


def main(argv: Optional[list[str]] = None) -> dict:
    """CLI 分发：默认 serve（双线常驻）；user / background 单跑调试。"""
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "serve"
    if cmd == "user":
        return run()
    if cmd == "background":
        return run_background()
    if cmd == "serve":
        return serve()
    raise SystemExit(f"未知命令：{cmd}（可选：serve / user / background）")


if __name__ == "__main__":
    main()
