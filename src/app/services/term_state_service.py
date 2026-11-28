# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""建页流程完成后，把状态直接同步到 radar_term_status。"""

from __future__ import annotations

from src.app.enums import CurationStatus
from src.app.tools.adapters.hotwords import state_sync


def after_plan(terms: list[str]) -> dict:
    names = [str(term) for term in terms if str(term or "").strip()]
    return state_sync.mark(names, CurationStatus.WIP.value)


def after_push(term: str) -> dict:
    value = str(term or "").strip()
    return state_sync.mark([value], CurationStatus.LIVE.value) if value else {
        "ok": True, "updated": 0, "changes": []
    }


def reconcile() -> dict:
    return state_sync.sync()
