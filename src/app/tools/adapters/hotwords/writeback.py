# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""把 Agent 分析结论写入统一 D1。"""

from __future__ import annotations

import json
import time
from typing import Any

from src.app.enums import CurationStatus, HintName, RejectSource, Verdict
from src.app.tools.adapters.cloudflare import database
from src.app.utils.term_key import term_key

_HINT_NAMES = {item.value for item in HintName}


def write_result(item: dict[str, Any], text: str, hints: list[str] | None,
                 verdict: str, reason: str = "") -> dict[str, Any]:
    term = str(item.get("term") or "").strip()
    key = term_key(item.get("key") or term)
    rows = database.query("SELECT term_id FROM radar_terms WHERE term_key = ? LIMIT 1", [key])
    if not rows:
        raise ValueError(f"词库中不存在热词：{term}")
    term_id = int(rows[0]["term_id"])

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    valid_hints = [value for value in (hints or []) if str(value) in _HINT_NAMES][:3]
    payload = {
        "text": text,
        "verdict": verdict,
        "reason": str(reason or "")[:300],
        "hints": valid_hints,
        "updated_at": now,
    }
    result_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    summary = next((line.strip() for line in text.splitlines() if line.strip()), "")[:300]
    prompt_version = str(item.get("promptVersion") or "v1")

    database.execute(
        """INSERT INTO radar_term_analyses
             (term_id, status, verdict, summary, result_json, prompt_version,
              result_version, started_at, completed_at)
           VALUES (?, 'completed', ?, ?, ?, ?, 'v1', ?, ?)
           ON CONFLICT(term_id) DO UPDATE SET
             status = 'completed',
             verdict = excluded.verdict,
             summary = excluded.summary,
             result_json = excluded.result_json,
             prompt_version = excluded.prompt_version,
             result_version = excluded.result_version,
             error_message = NULL,
             completed_at = excluded.completed_at""",
        [term_id, verdict, summary, result_json, prompt_version, now, now],
    )

    next_status = CurationStatus.TODO.value if verdict == Verdict.DO.value else "new"
    rejected_at = now if verdict == Verdict.SKIP.value else None
    rejected_reason = (str(reason or "AI 判定不做")[:300]
                       if verdict == Verdict.SKIP.value else None)
    rejected_source = RejectSource.AI.value if verdict == Verdict.SKIP.value else None
    hints_json = json.dumps(valid_hints, ensure_ascii=False) if hints is not None else None

    database.execute(
        """INSERT INTO radar_term_status
             (term_id, status, is_favorite, hints_override_json,
              rejected_at, rejected_reason, rejected_source)
           VALUES (?, ?, 0, ?, ?, ?, ?)
           ON CONFLICT(term_id) DO UPDATE SET
             hints_override_json = COALESCE(excluded.hints_override_json,
                                             radar_term_status.hints_override_json),
             status = CASE
               WHEN excluded.status = 'todo'
                 AND NOT (radar_term_status.rejected_source = 'manual'
                          AND radar_term_status.rejected_at IS NOT NULL)
                 THEN 'todo'
               WHEN excluded.rejected_source = 'ai'
                 THEN CASE WHEN radar_term_status.status IN ('wip', 'live', 'parked')
                           THEN radar_term_status.status ELSE 'new' END
               ELSE radar_term_status.status
             END,
             is_favorite = CASE
               WHEN excluded.rejected_source = 'ai'
                    AND radar_term_status.status NOT IN ('wip', 'live', 'parked')
                 THEN 0
               ELSE radar_term_status.is_favorite
             END,
             rejected_at = CASE
               WHEN excluded.rejected_source = 'ai'
                 THEN CASE WHEN radar_term_status.rejected_source = 'manual'
                                AND radar_term_status.rejected_at IS NOT NULL
                           THEN radar_term_status.rejected_at ELSE excluded.rejected_at END
               WHEN excluded.status = 'todo' AND radar_term_status.rejected_source = 'ai'
                 THEN NULL
               ELSE radar_term_status.rejected_at
             END,
             rejected_reason = CASE
               WHEN excluded.rejected_source = 'ai'
                 THEN CASE WHEN radar_term_status.rejected_source = 'manual'
                                AND radar_term_status.rejected_at IS NOT NULL
                           THEN radar_term_status.rejected_reason ELSE excluded.rejected_reason END
               WHEN excluded.status = 'todo' AND radar_term_status.rejected_source = 'ai'
                 THEN NULL
               ELSE radar_term_status.rejected_reason
             END,
             rejected_source = CASE
               WHEN excluded.rejected_source = 'ai'
                 THEN CASE WHEN radar_term_status.rejected_source = 'manual'
                                AND radar_term_status.rejected_at IS NOT NULL
                           THEN radar_term_status.rejected_source ELSE 'ai' END
               WHEN excluded.status = 'todo' AND radar_term_status.rejected_source = 'ai'
                 THEN NULL
               ELSE radar_term_status.rejected_source
             END""",
        [term_id, next_status, hints_json, rejected_at, rejected_reason, rejected_source],
    )

    status = ("rejected" if verdict == Verdict.SKIP.value
              else CurationStatus.TODO.value if verdict == Verdict.DO.value else "")
    return {
        "status": status,
        "textChars": len(text),
        "textPreview": text[:200],
        "writes": [
            {"table": "radar_term_analyses", "ok": True},
            {"table": "radar_term_status", "ok": True},
        ],
        "writeErrors": [],
    }
