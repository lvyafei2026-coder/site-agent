# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；详见 LICENSE.

"""热词状态闭环的服务门面：建页线各节点只认识本模块，不伸手抓 adapters。

业务规则（状态映射 / 只升级不降级 / 不动未收藏词）在 adapters/hotwords/state_sync，
本层只负责「在什么时候触发」与「失败不让主链路崩」：

    after_plan(terms)   排产落 planned 后 → 这些词置 wip（开发中）
    after_push(term)    建页推送落 deployed 后 → 该词置 live（已上线）
    reconcile()         手动全量对账：把历史台账状态补写回 saved 表

同步失败一律只记日志：台账和 saved 表由 KV 最终一致串起来，偶尔一次写失败
下次排产/建页或对账会补上，不该让已经成功的排产/建页反过来报错。
"""

from __future__ import annotations

from typing import Any

from src.app.enums import CurationStatus
from src.app.tools.adapters.hotwords import state_sync
from src.app.utils.logger import get_logger

log = get_logger(__name__)


def _safe(out: dict, event: str) -> dict:
    if not out.get("ok"):
        log.warning(event, extra={"error": out.get("error", ""),
                                  "updated": out.get("updated", 0)})
    return out


def after_plan(terms: list[str]) -> dict:
    """排产写入 planned 后调用：入选词从 todo 升到 wip（开发中）。"""
    names = [str(t) for t in terms if str(t or "").strip()]
    if not names:
        return {"ok": True, "updated": 0, "changes": []}
    return _safe(state_sync.mark(names, CurationStatus.WIP.value),
                 "term_state_after_plan_failed")


def after_push(term: str) -> dict:
    """git_push 成功后调用：该词升到 live（已上线）。"""
    if not str(term or "").strip():
        return {"ok": True, "updated": 0, "changes": []}
    return _safe(state_sync.mark([str(term)], CurationStatus.LIVE.value),
                 "term_state_after_push_failed")


def reconcile() -> dict:
    """全量对账：读台账把历史遗留词补回正确状态（供 console 手动触发）。"""
    return _safe(state_sync.sync(), "term_state_reconcile_failed")
