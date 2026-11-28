# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Radar D1 词库与候选池查询。业务状态只从 radar-db 读取。"""

from __future__ import annotations

import json
import re
from typing import Any

from src.app.enums import CandidateStatus, CurationStatus, RejectSource
from src.app.tools.adapters.cloudflare import database
from src.app.utils.term_key import term_key

EXAMPLE_LIMIT = 8
_EXAMPLE_SKIP_RX = re.compile(r"搜索意图不是找工具|热度下降|趋势下降|未填原因|旧版本本地标记")


def _json(value: str | None, default: Any):
    return default if value is None else json.loads(value)


def _project(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": str(row["key"]),
        "term": str(row["term"]),
        "related": _json(row.get("related_json"), [])[:6],
        "hints": _json(row.get("hints_json"), [])[:6],
        "geos": _json(row.get("geos_json"), [])[:16],
        "geoCount": int(row.get("geo_count") or 0),
        "traffic": int(float(row.get("traffic") or 0)),
        "count": int(row.get("survival_rounds") or 0),
        "score": round(float(row.get("score") or 0)),
        "src": str(row.get("source") or "trends"),
        "verdict": row.get("verdict") or "",
    }


def _meta() -> dict[str, Any]:
    rows = database.query(
        """SELECT run_id, status, started_at, finished_at, metadata_json
           FROM radar_runs ORDER BY started_at DESC LIMIT 1"""
    )
    if not rows:
        return {}
    row = rows[0]
    meta = _json(row.get("metadata_json"), {})
    meta["runId"] = row["run_id"]
    meta["status"] = row["status"]
    meta["updated"] = row.get("finished_at") or row.get("started_at")
    return meta


def _interests(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {}
    keys = [item["key"] for item in items]
    placeholders = ",".join("?" for _ in keys)
    rows = database.query(
        f"""SELECT t.term_key, t.term, a.trend_json
            FROM radar_term_analyses a
            JOIN radar_terms t ON t.term_id = a.term_id
            WHERE t.term_key IN ({placeholders}) AND a.trend_json IS NOT NULL""",
        keys,
    )
    out = {}
    for row in rows:
        payload = _json(row["trend_json"], {})
        out[row["term_key"]] = {
            "term": row["term"],
            "summary": payload.get("summary") or {},
            "at": payload.get("at"),
            "points": [point.get("v") for point in payload.get("points", [])],
        }
    return out


def _candidate_rows(limit: int) -> list[dict[str, Any]]:
    return database.query(
        """SELECT t.term_key AS key, t.term, c.score,
                  COALESCE(s.hints_override_json, c.signals_json, t.hints_json, '[]') AS hints_json,
                  COALESCE(c.geos_json, '[]') AS geos_json,
                  COALESCE(c.related_json, t.related_json, '[]') AS related_json,
                  c.traffic, c.geo_count, c.survival_rounds, t.source, a.verdict
           FROM radar_candidates c
           JOIN radar_terms t ON t.term_id = c.term_id
           LEFT JOIN radar_term_status s ON s.term_id = t.term_id
           LEFT JOIN radar_term_analyses a ON a.term_id = t.term_id
           WHERE c.is_active = 1
             AND COALESCE(s.status, 'new') = 'new'
             AND COALESCE(s.is_favorite, 0) = 0
             AND COALESCE(s.tag, '') = ''
             AND s.rejected_at IS NULL
           ORDER BY c.score DESC, c.last_seen_at DESC
           LIMIT ?""",
        [limit],
    )


def _status_rows(status: str, limit: int) -> list[dict[str, Any]]:
    if status == CurationStatus.SAVED.value:
        condition = "(s.is_favorite = 1 OR s.status = 'watching')"
        params: list[Any] = [limit]
    else:
        condition = "s.status = ?"
        params = [status, limit]
    return database.query(
        f"""SELECT t.term_key AS key, t.term, c.score,
                   COALESCE(s.hints_override_json, c.signals_json, t.hints_json, '[]') AS hints_json,
                   COALESCE(c.geos_json, '[]') AS geos_json,
                   COALESCE(c.related_json, t.related_json, '[]') AS related_json,
                   c.traffic, c.geo_count, c.survival_rounds, t.source, a.verdict
            FROM radar_term_status s
            JOIN radar_terms t ON t.term_id = s.term_id
            LEFT JOIN radar_candidates c ON c.term_id = t.term_id
            LEFT JOIN radar_term_analyses a ON a.term_id = t.term_id
            WHERE {condition}
            ORDER BY s.updated_at DESC
            LIMIT ?""",
        params,
    )


def snapshot(status: str = CandidateStatus.DOING.value, limit: int = 40,
             light: bool = False) -> dict[str, Any]:
    """读取当前候选池或指定人工跟进状态，不回落到其他数据源。"""
    rows = (_candidate_rows(limit or 10000)
            if status == CandidateStatus.DOING.value
            else _status_rows(status, limit or 10000))
    items = [_project(row) for row in rows]
    return {
        "items": items,
        "meta": _meta(),
        "interests": {} if light else _interests(items),
        **({"query": {"status": status, "matched": len(items)}}
           if status != CandidateStatus.DOING.value else {}),
    }


def pick_examples(rows: list[dict[str, Any]], limit: int = EXAMPLE_LIMIT) -> list[dict[str, str]]:
    """从人工不做记录中挑少量可泛化判例。"""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in sorted(rows, key=lambda x: x.get("updatedAt") or x.get("at") or "", reverse=True):
        if row.get("source") != RejectSource.MANUAL.value:
            continue
        term = str(row.get("term") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if not term or len(reason) < 4 or _EXAMPLE_SKIP_RX.search(reason):
            continue
        normalized = re.sub(r"[\s，。；、,.;:：]+$", "", reason)
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append({"term": term[:60], "reason": reason[:120]})
        if len(out) >= max(0, min(int(limit), 20)):
            break
    return out


def read_examples() -> list[dict[str, str]]:
    rows = database.query(
        """SELECT t.term, s.rejected_reason AS reason,
                  s.rejected_source AS source, s.rejected_at AS at,
                  s.updated_at AS updatedAt
           FROM radar_term_status s
           JOIN radar_terms t ON t.term_id = s.term_id
           WHERE s.rejected_at IS NOT NULL AND s.rejected_source = ?
           ORDER BY s.updated_at DESC LIMIT 50""",
        [RejectSource.MANUAL.value],
    )
    return pick_examples(rows)


def select_pending(seen: set[str]) -> dict[str, Any] | None:
    """取一个未跟进、未分析且本轮未处理过的活动候选词。"""
    params: list[Any] = []
    excluded = ""
    if seen:
        keys = sorted({term_key(key) for key in seen if term_key(key)})
        if keys:
            excluded = f" AND t.term_key NOT IN ({','.join('?' for _ in keys)})"
            params.extend(keys)
    rows = database.query(
        f"""SELECT t.term_key AS key, t.term, c.score,
                   COALESCE(s.hints_override_json, c.signals_json, t.hints_json, '[]') AS hints_json,
                   COALESCE(c.geos_json, '[]') AS geos_json,
                   COALESCE(c.related_json, t.related_json, '[]') AS related_json,
                   c.traffic, c.geo_count, c.survival_rounds, t.source, a.verdict
            FROM radar_candidates c
            JOIN radar_terms t ON t.term_id = c.term_id
            LEFT JOIN radar_term_status s ON s.term_id = t.term_id
            LEFT JOIN radar_term_analyses a ON a.term_id = t.term_id
            WHERE c.is_active = 1
              AND COALESCE(s.status, 'new') = 'new'
              AND COALESCE(s.is_favorite, 0) = 0
              AND COALESCE(s.tag, '') = ''
              AND s.rejected_at IS NULL
              AND (a.term_id IS NULL OR a.status IN ('queued', 'failed'))
              {excluded}
            ORDER BY c.score DESC, c.last_seen_at DESC
            LIMIT 1""",
        params,
    )
    return _project(rows[0]) if rows else None


def candidate_by_term(term: str) -> dict[str, Any] | None:
    """按 term_key 从全量词库点名读取词条，不受候选池状态限制。"""
    key = term_key(term)
    if not key:
        return None
    rows = database.query(
        """SELECT t.term_key AS key, t.term, c.score,
                  COALESCE(s.hints_override_json, c.signals_json, t.hints_json, '[]') AS hints_json,
                  COALESCE(c.geos_json, '[]') AS geos_json,
                  COALESCE(c.related_json, t.related_json, '[]') AS related_json,
                  c.traffic, c.geo_count, c.survival_rounds, t.source, a.verdict
           FROM radar_terms t
           LEFT JOIN radar_candidates c ON c.term_id = t.term_id
           LEFT JOIN radar_term_status s ON s.term_id = t.term_id
           LEFT JOIN radar_term_analyses a ON a.term_id = t.term_id
           WHERE t.term_key = ? LIMIT 1""",
        [key],
    )
    return _project(rows[0]) if rows else None
