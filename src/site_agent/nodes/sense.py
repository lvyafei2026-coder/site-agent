# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""感知节点：把任务需要的原始数据收集到 observations。不调 LLM。"""

from __future__ import annotations

import logging
from typing import Any

from ..llm.breaker import BreakerPaused, ensure_alive
from ..state import OpsState
from ..tasks import get_task

log = logging.getLogger(__name__)


def sense(state: OpsState) -> dict[str, Any]:
    task_type = state["task_type"]
    try:
        ensure_alive(task_type)
    except BreakerPaused:
        # 熔断中：给空 observations，让 decide 走"无动作"分支，避免继续消耗
        return {"observations": [], "actions": [], "errors": [f"{task_type} 熔断中"]}

    mod = get_task(task_type)
    obs = mod.sense(state)
    log.info("sense 完成 task=%s 条目=%s", task_type, len(obs))
    return {"observations": obs, "retry": state.get("retry", 0)}
