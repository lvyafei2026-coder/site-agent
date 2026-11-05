# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""恢复被 interrupt() 挂起的任务。

两种触发方式：
1. IMAP 轮询（生产）：人回复审批邮件，写 yes / no
2. 命令行（调试/兜底）：uv run site-agent-approve <thread_id> yes|no
"""

from __future__ import annotations

import logging
import sys

from langgraph.types import Command

from .graph import get_app

log = logging.getLogger(__name__)


def resume(thread_id: str, approved: bool) -> dict:
    app = get_app()
    log.info("恢复任务 %s approved=%s", thread_id, approved)
    return app.invoke(
        Command(resume=approved),
        config={"configurable": {"thread_id": thread_id}},
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) != 3:
        print("用法：site-agent-approve <thread_id> yes|no")
        raise SystemExit(2)
    thread_id, decision = sys.argv[1], sys.argv[2].lower()
    if decision not in ("yes", "y", "no", "n"):
        print("决定只能是 yes 或 no")
        raise SystemExit(2)
    resume(thread_id, decision.startswith("y"))


if __name__ == "__main__":
    main()
