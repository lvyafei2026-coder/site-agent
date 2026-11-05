# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""执行节点：写操作前 interrupt() 等人工确认；动作按 key 去重。

注意：LangGraph 在 resume 时会从头重跑本节点，因此：
1. execute_action 必须幂等（gen_page 查 D1 去重、promo 查排期去重）
2. 审批邮件用 KV 标记只发一次，避免 resume 时重发
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from ..cf import kv
from ..llm.breaker import record
from ..notify import mail
from ..state import Action, OpsState
from ..tasks import get_task

log = logging.getLogger(__name__)

DONE_STATES = {"deployed", "published", "scheduled", "ready-to-post", "skipped", "rejected"}


def _needs_approval(action: Action) -> bool:
    return action.get("risk") == "write" and not action.get("auto", False)


def _notify_once(thread_id: str, action: Action) -> None:
    flag = f"approval-sent:{action.get('key', '')}"
    try:
        if kv.get(flag):
            return
        mail.send_approval_request(thread_id, dict(action))
        kv.put(flag, 1, ttl_sec=7 * 86400)
    except Exception as exc:  # noqa: BLE001 - 通知失败不阻塞挂起
        log.warning("审批通知失败：%s", exc)


def execute(state: OpsState) -> dict[str, Any]:
    task_type = state["task_type"]
    mod = get_task(task_type)
    thread_id = state.get("payload", {}).get("thread_id", "")

    executed: list[dict[str, Any]] = list(state.get("executed") or [])
    done = {e.get("key") for e in executed if e.get("status") in DONE_STATES}

    for action in state.get("actions") or []:
        key = action.get("key", "")
        if key in done:
            continue

        if _needs_approval(action):
            _notify_once(thread_id, action)
            approved = interrupt(
                {
                    "thread_id": thread_id,
                    "key": key,
                    "summary": action.get("summary", ""),
                    "payload": action.get("payload", {}),
                }
            )
            if not approved:
                executed.append({"key": key, "status": "rejected", "detail": "人工拒绝"})
                continue

        try:
            result = mod.execute_action(action)
        except Exception as exc:  # noqa: BLE001 - 计入熔断后继续下一个动作
            log.exception("执行失败 key=%s", key)
            record(task_type, ok=False, error=f"{key}: {exc}")
            executed.append({"key": key, "status": "failed", "detail": str(exc)[:300]})
            continue
        executed.append(result)

    return {"executed": executed}
