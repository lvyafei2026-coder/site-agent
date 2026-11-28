# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""把 agent_pages 的建页进度同步到 radar_term_status。"""

from __future__ import annotations

from typing import Any

from src.app.enums import CurationStatus, PageQueryMode, PageStatus
from src.app.tools.adapters.cloudflare import database
from src.app.tools.adapters.cloudflare.sql import PAGE_QUERIES
from src.app.utils.term_key import term_key

_RANK = {
    CurationStatus.SAVED.value: 0,
    "watching": 0,
    CurationStatus.TODO.value: 1,
    CurationStatus.WIP.value: 2,
    CurationStatus.LIVE.value: 3,
    "done": 4,
    "ignored": 4,
    CurationStatus.PARKED.value: 4,
}
_TARGET = {
    PageStatus.PLANNED.value: CurationStatus.WIP.value,
    PageStatus.GENERATED.value: CurationStatus.LIVE.value,
    PageStatus.DEPLOYED.value: CurationStatus.LIVE.value,
    PageStatus.INDEXED.value: CurationStatus.LIVE.value,
    PageStatus.VERIFIED.value: CurationStatus.LIVE.value,
}


def norm_term(term: Any) -> str:
    return term_key(str(term or "").replace("-", " ").replace("_", " "))


def project(rows: list[dict], statuses: list[dict]) -> tuple[list[dict], list[dict]]:
    targets: dict[str, str] = {}
    for row in rows:
        key = norm_term(row.get("keyword"))
        target = _TARGET.get(str(row.get("status") or ""))
        if key and target and _RANK[target] > _RANK.get(targets.get(key, ""), -1):
            targets[key] = target

    updates: list[dict] = []
    changes: list[dict] = []
    for row in statuses:
        key = norm_term(row.get("term_key") or row.get("term"))
        current = str(row.get("status") or "new")
        target = targets.get(key)
        if not target or current in (CurationStatus.PARKED.value, "done", "ignored"):
            continue
        if _RANK.get(target, -1) <= _RANK.get(current, -1):
            continue
        updates.append({"term_id": int(row["term_id"]), "status": target})
        changes.append({"term": row.get("term") or "", "key": key,
                        "from": current, "to": target})
    return updates, changes


def read_ledger() -> list[dict]:
    query = PAGE_QUERIES[PageQueryMode.ALL.value]
    return database.query(f"{query.sql} LIMIT 1000")


def sync(rows: list[dict] | None = None) -> dict:
    source = read_ledger() if rows is None else rows
    targets = sorted({norm_term(row.get("keyword")) for row in source
                      if _TARGET.get(str(row.get("status") or ""))})
    if not targets:
        return {"ok": True, "updated": 0, "changes": []}

    placeholders = ",".join("?" for _ in targets)
    statuses = database.query(
        f"""SELECT t.term_id, t.term_key, t.term, s.status
            FROM radar_term_status s
            JOIN radar_terms t ON t.term_id = s.term_id
            WHERE t.term_key IN ({placeholders})""",
        targets,
    )
    updates, changes = project(source, statuses)
    for update in updates:
        database.execute(
            "UPDATE radar_term_status SET status = ? WHERE term_id = ?",
            [update["status"], update["term_id"]],
        )
    return {"ok": True, "updated": len(changes), "changes": changes}


def mark(terms: list[str], status: str) -> dict:
    if status not in _RANK or status in (CurationStatus.SAVED.value, CurationStatus.PARKED.value):
        return {"ok": False, "updated": 0, "changes": []}
    page_status = {
        CurationStatus.WIP.value: PageStatus.PLANNED.value,
        CurationStatus.LIVE.value: PageStatus.DEPLOYED.value,
    }.get(status)
    if not page_status:
        return {"ok": False, "updated": 0, "changes": []}
    return sync([{"keyword": term, "status": page_status} for term in terms])
