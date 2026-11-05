# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""反馈节点：把结果写回 D1 台账 / 发报告。"""

from __future__ import annotations

import logging
from typing import Any

from ..llm.breaker import record
from ..state import OpsState
from ..tasks import get_task

log = logging.getLogger(__name__)


def learn(state: OpsState) -> dict[str, Any]:
    task_type = state["task_type"]
    mod = get_task(task_type)
    try:
        result = mod.learn(state)
    except Exception as exc:  # noqa: BLE001 - 台账写入失败要能被发现
        log.exception("learn 写入失败")
        record(task_type, ok=False, error=f"learn: {exc}")
        return {"result": {"error": str(exc)}}

    log.info("learn 完成 task=%s %s", task_type, result)
    return {"result": result}
