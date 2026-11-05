# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""验证节点：检查执行结果，失败累计触发熔断。"""

from __future__ import annotations

import logging
from typing import Any

from ..llm.breaker import record
from ..state import OpsState
from ..tasks import get_task

log = logging.getLogger(__name__)


def verify(state: OpsState) -> dict[str, Any]:
    task_type = state["task_type"]
    mod = get_task(task_type)

    try:
        checks = mod.verify(state)
    except Exception as exc:  # noqa: BLE001
        log.exception("验证节点异常")
        record(task_type, ok=False, error=str(exc))
        return {"checks": [], "errors": [str(exc)], "retry": state.get("retry", 0) + 1}

    failed = [c for c in checks if not c.get("ok")]
    if failed:
        record(task_type, ok=False, error=f"{len(failed)} 项校验失败")
        return {
            "checks": checks,
            "errors": [c.get("detail", "") for c in failed],
            "retry": state.get("retry", 0) + 1,
        }

    record(task_type, ok=True)
    return {"checks": checks}
