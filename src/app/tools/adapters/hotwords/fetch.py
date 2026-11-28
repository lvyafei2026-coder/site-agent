# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""SERP / Trends 取数与 D1 缓存。"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from src.app.observability.langsmith import note
from src.app.tools.adapters.cloudflare import database
from src.app.utils.source_errors import SourceUnavailable
from src.app.utils.term_key import term_key
from . import scrape_serp, scrape_trend

log = logging.getLogger(__name__)

SERP_GAP_SEC = 1.5
SERP_TTL = 604800
TREND_TTL = 259200

_serp_lock = threading.Lock()
_last_serp_at = 0.0


def _term_id(term: str) -> int:
    rows = database.query("SELECT term_id FROM radar_terms WHERE term_key = ? LIMIT 1", [term_key(term)])
    if not rows:
        raise ValueError(f"词库中不存在热词：{term}")
    return int(rows[0]["term_id"])


def _cached(term_id: int, column: str, ttl: int) -> dict[str, Any] | None:
    rows = database.query(
        f"SELECT {column} AS payload FROM radar_term_analyses WHERE term_id = ? LIMIT 1",
        [term_id],
    )
    if not rows or not rows[0].get("payload"):
        return None
    payload = json.loads(rows[0]["payload"])
    if time.time() * 1000 - int(payload.get("at") or 0) > ttl * 1000:
        return None
    return payload


def _save(term_id: int, column: str, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    database.execute(
        f"""INSERT INTO radar_term_analyses (term_id, status, {column})
            VALUES (?, 'queued', ?)
            ON CONFLICT(term_id) DO UPDATE SET
              {column} = excluded.{column}""",
        [term_id, encoded],
    )


def serp(term: str) -> dict[str, Any]:
    """读取七天内的 D1 SERP 缓存；过期后重新抓取。"""
    term_id = _term_id(term)
    cached = _cached(term_id, "serp_json", SERP_TTL)
    if cached:
        return {
            "ok": True, "data": cached, "error": "", "fromCache": True,
            "engine": cached.get("engine", ""),
            "itemCount": len(cached.get("items") or []),
            "competition": cached.get("competition") or {},
            "writes": [], "writeErrors": [],
        }

    global _last_serp_at
    with _serp_lock:
        wait = _last_serp_at + SERP_GAP_SEC - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_serp_at = time.monotonic()

    try:
        result = scrape_serp.fetch_serp(term)
    except SourceUnavailable:
        raise
    except Exception as exc:
        log.warning("SERP 失败 %s: %s", term, exc)
        note(fetchError=str(exc)[:300])
        return {
            "ok": False, "data": None, "error": str(exc)[:120],
            "fromCache": False, "engine": "", "itemCount": 0,
            "competition": {}, "writes": [], "writeErrors": [],
        }

    note(engine=result.get("engine"), attempts=result.get("attempts") or [])
    competition = scrape_serp.score_competition(result["items"], term)
    payload = {
        "engine": result["engine"],
        "items": result["items"],
        "competition": competition,
        "at": int(time.time() * 1000),
    }
    _save(term_id, "serp_json", payload)
    return {
        "ok": True, "data": payload, "error": "", "fromCache": False,
        "engine": payload["engine"], "itemCount": len(payload["items"]),
        "competition": competition,
        "writes": [{"table": "radar_term_analyses.serp_json", "ok": True}],
        "writeErrors": [],
    }


def trend(term: str) -> dict[str, Any]:
    """读取三天内的 D1 Trends 缓存；过期后重新抓取。"""
    term_id = _term_id(term)
    cached = _cached(term_id, "trend_json", TREND_TTL)
    if cached:
        return {
            "ok": True, "data": cached, "error": "", "fromCache": True,
            "pointCount": len(cached.get("points") or []),
            "writes": [], "writeErrors": [],
        }

    try:
        result = scrape_trend.fetch_interest(term)
    except SourceUnavailable:
        raise
    except Exception as exc:
        log.warning("TREND 失败 %s: %s", term, exc)
        note(fetchError=str(exc)[:300])
        return {
            "ok": False, "data": None, "error": str(exc)[:120],
            "fromCache": False, "pointCount": 0, "writes": [], "writeErrors": [],
        }

    note(attempts=result.get("attempts") or [],
         pointCount=len(result.get("points") or []),
         summary=result.get("summary") or {})
    payload = {
        "points": result["points"],
        "summary": result["summary"],
        "at": int(time.time() * 1000),
    }
    _save(term_id, "trend_json", payload)
    return {
        "ok": True, "data": payload, "error": "", "fromCache": False,
        "pointCount": len(payload["points"]),
        "writes": [{"table": "radar_term_analyses.trend_json", "ok": True}],
        "writeErrors": [],
    }
