# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""决策节点：唯一调用 LLM 的地方。输出结构化 actions。"""

from __future__ import annotations

import logging
from typing import Any

from ..llm import gateway
from ..state import OpsState
from ..tasks import get_task

log = logging.getLogger(__name__)


def decide(state: OpsState) -> dict[str, Any]:
    if not state.get("observations"):
        return {"decision": {"summary": "无数据或已熔断"}, "actions": []}

    mod = get_task(state["task_type"])
    decision = gateway.json_call(
        mod.LLM_PROFILE,
        gateway.load_prompt(mod.PROMPT),
        mod.build_user_prompt(state),
    )
    actions = mod.parse(decision, state)
    log.info("decide 完成 task=%s actions=%s", state["task_type"], len(actions))
    return {"decision": decision, "actions": actions}
