# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的外部取数层：SERP / Trends 读缓存 → 缺失则现抓 → 写回共享 KV。

只负责「补数据」，不碰判据 / LLM / 结论回写（判据在 criteria，回写在
writeback，谁来调这一步是 graph/hotword_graph.py 的连线）。

★源级不可用（连不上 / 被挑战页占满）原样抛 SourceUnavailable：工具契约把它判成
  retryable=True 的失败，取数节点据此让后续词跳过该源，不中止整轮。其它异常在本层
  降级成「无数据 + error」，不拦 AI。
★本层不打 tool span：span 由 tools/registry.py 的统一 tracer 建（tool:fetch_serp /
  tool:fetch_trend），这里只用 note() 把抓取诊断挂到当前 span。
★KV 句柄自己取（get_kv()），与 wordlib.snapshot 一致，调用方不传句柄；写入明细
  （writes / writeErrors）随结果返回，由取数节点汇总进该词的 error。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from src.app.enums import AnalysisPhase
from src.app.observability.langsmith import note
from src.app.tools.adapters.cloudflare.kv import (
    term_key, get_kv,
    SERP_PREFIX, TREND_PREFIX, COMPETITION_KEY, INTERESTS_KEY,
)
from .kv_io import _WriteLog, _kv_dict

# 模块引用（不是 from ... import fetch_*）：测试 monkeypatch.setattr(scrape_serp, ...)
# patch 的是模块对象属性，所有共享该对象的地方都会生效。
from . import scrape_serp, scrape_trend
from .source_errors import SourceUnavailable

log = logging.getLogger(__name__)

SERP_GAP_SEC = 1.5          # 全局串行 + 最小间隔；202 的根因是请求头，见 scrape_serp.py
SERP_TTL = 604800           # serp:v1: 缓存 7 天
TREND_TTL = 259200          # trend:v1: 缓存 3 天

_serp_lock = threading.Lock()
_last_serp_at = 0.0


def serp(term: str) -> dict[str, Any]:
    """KV 已有 serp:v1: 直接读；缺失则现抓并写回。全局串行避免并发同时打 DDG。

    返回 {ok, data, error, fromCache, engine, itemCount, competition, writes, writeErrors}：
    与 trend 同一形状（证据步骤的统一契约），data 是完整 payload，判据层拿它算辨形摘要。
    """
    kv = get_kv()
    key = term_key(term)
    writes = _WriteLog(term)
    writes.phase = AnalysisPhase.SERP.value
    out: dict[str, Any] = {"ok": False, "data": None, "error": "", "fromCache": False,
                           "engine": "", "itemCount": 0, "competition": {},
                           "writes": [], "writeErrors": []}

    cached = kv.get_json(SERP_PREFIX + key)
    if cached:
        out.update(ok=True, data=cached, fromCache=True, engine=cached.get("engine", ""),
                   itemCount=len(cached.get("items") or []),
                   competition=cached.get("competition") or {})
        return out

    global _last_serp_at
    with _serp_lock:
        wait = _last_serp_at + SERP_GAP_SEC - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_serp_at = time.monotonic()
    try:
        s = scrape_serp.fetch_serp(term)
    except SourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 单词级问题（解析等）不拦 AI
        log.warning("SERP 失败 %s: %s", term, exc)
        note(fetchError=str(exc)[:300])
        out["error"] = str(exc)[:120]
        return out
    note(engine=s.get("engine"), attempts=s.get("attempts") or [])
    comp = scrape_serp.score_competition(s["items"], term)
    payload = {"engine": s["engine"], "items": s["items"], "competition": comp, "at": int(time.time() * 1000)}
    # 单词缓存键：各词独立、无 read-modify-write，但仍要检查写返回值
    writes.json(kv, SERP_PREFIX + key, payload, ttl=SERP_TTL)
    # competition 是整表：读-改-写必须走原子更新
    entry = {"score": comp["score"], "level": comp["level"], "at": payload["at"]}
    writes.table(kv, COMPETITION_KEY, lambda m: {**m, key: entry}, reader=_kv_dict)
    out.update(ok=True, data=payload, engine=payload["engine"], itemCount=len(payload["items"]),
               competition=comp, writes=writes.records, writeErrors=writes.failed)
    return out


def trend(term: str) -> dict[str, Any]:
    """KV 已有 trend:v1: 直接读；缺失则现抓并写回 + 更新 interests 摘要表。

    返回 {ok, data, error, fromCache, pointCount, writes, writeErrors}（与 serp 同一形状）。
    ★源级不可用抛 SourceUnavailable：趋势只是辅助证据，不该因为它连不上就让整轮
      分析白跑，但也不该每个词都去撞一遍超时——降级置位由编排层决定。
    """
    kv = get_kv()
    key = term_key(term)
    writes = _WriteLog(term)
    writes.phase = AnalysisPhase.TREND.value
    out: dict[str, Any] = {"ok": False, "data": None, "error": "", "fromCache": False,
                           "pointCount": 0, "writes": [], "writeErrors": []}

    cached = kv.get_json(TREND_PREFIX + key)
    if cached:
        out.update(ok=True, data=cached, fromCache=True,
                   pointCount=len(cached.get("points") or []))
        return out

    try:
        t = scrape_trend.fetch_interest(term)
    except SourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 单词解析问题不影响结论
        log.warning("TREND 失败 %s: %s", term, exc)
        note(fetchError=str(exc)[:300])
        out["error"] = str(exc)[:120]
        return out
    note(attempts=t.get("attempts") or [], pointCount=len(t.get("points") or []),
         summary=t.get("summary") or {})
    payload = {"points": t["points"], "summary": t["summary"], "at": int(time.time() * 1000)}
    writes.json(kv, TREND_PREFIX + key, payload, ttl=TREND_TTL)
    entry = {
        "term": term, "summary": t["summary"], "at": payload["at"],
        "points": [p["v"] for p in t["points"]],
    }
    writes.table(kv, INTERESTS_KEY, lambda m: {**m, key: entry}, reader=_kv_dict)
    out.update(ok=True, data=payload, pointCount=len(t["points"]),
               writes=writes.records, writeErrors=writes.failed)
    return out
