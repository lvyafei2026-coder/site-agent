# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""进程入口：APScheduler 注册 cron，按任务类型触发 LangGraph。"""

from __future__ import annotations

import logging
import sys
from datetime import date

from apscheduler.schedulers.blocking import BlockingScheduler

from .approvals import poll_once
from .graph import get_app
from .llm.breaker import BreakerPaused
from .resume import resume

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("site_agent")

REQUIRED_ENV = (
    "CF_ACCOUNT_ID",
    "CF_API_TOKEN",
    "D1_DATABASE_ID",
    "GITHUB_TOKEN",
    "GH_ORG",
    "LLM_API_KEY",
)


def run_task(task_type: str, payload: dict | None = None) -> dict:
    """thread_id = 业务主键：重复触发只续跑，不会重复建页/重复发信。"""
    tid = f"{task_type}:{date.today().isoformat()}"
    body = {"thread_id": tid, **(payload or {})}
    app = get_app()
    log.info("▶ 启动任务 %s", tid)
    try:
        result = app.invoke(
            {"task_type": task_type, "payload": body},
            config={"configurable": {"thread_id": tid}},
        )
        log.info("✔ 任务结束 %s → %s", tid, result.get("result"))
        return result
    except BreakerPaused as exc:
        log.warning("⏸ 任务被熔断跳过 %s：%s", tid, exc)
        return {"skipped": True}
    except Exception:  # noqa: BLE001 - 单任务异常不能拖垮调度器
        log.exception("✘ 任务失败 %s", tid)
        return {"error": True}


def job_gen_page() -> None:
    run_task("gen-page")


def job_daily_report() -> None:
    run_task("daily-report")


def job_backlink() -> None:
    run_task("backlink")


def job_promo() -> None:
    run_task("promo")


def job_approvals() -> None:
    try:
        handled = poll_once(resume)
        if handled:
            log.info("处理审批回复 %s 条", handled)
    except Exception:  # noqa: BLE001
        log.exception("审批轮询失败")


def check_env() -> list[str]:
    import os

    return [k for k in REQUIRED_ENV if not os.environ.get(k)]


def main() -> None:
    missing = check_env()
    if missing:
        log.error("缺少环境变量：%s（参考 .env.example）", ", ".join(missing))
        raise SystemExit(1)

    # 预热一次，尽早暴露 checkpointer / 依赖问题
    get_app()

    sched = BlockingScheduler(timezone="Asia/Shanghai")
    sched.add_job(job_gen_page, "cron", hour=8, minute=10, id="gen-page",
                  misfire_grace_time=3600, coalesce=True)
    sched.add_job(job_daily_report, "cron", hour=9, minute=0, id="daily-report",
                  misfire_grace_time=3600, coalesce=True)
    sched.add_job(job_backlink, "cron", day_of_week="mon,wed,fri", hour=10, minute=0,
                  id="backlink", misfire_grace_time=3600, coalesce=True)
    sched.add_job(job_promo, "cron", day_of_week="sun", hour=20, minute=0, id="promo",
                  misfire_grace_time=3600, coalesce=True)
    sched.add_job(job_approvals, "interval", minutes=5, id="approvals",
                  coalesce=True, max_instances=1)

    log.info("site-agent 已启动，等待调度（TZ=Asia/Shanghai）")
    log.info("下一次触发的任务：%s", [(j.id, str(j.next_run_time)) for j in sched.get_jobs()])
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("收到退出信号，调度器停止")
        sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
