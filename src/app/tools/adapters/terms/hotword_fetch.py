# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的外部取数层：SERP / Trends 读缓存 → 缺失则现抓 → 写回共享 KV。

拆分自 hotwords.py（2026-11-19 三层拆分）。只负责「补数据」这一步，不碰判据、
LLM 与结论回写（那些在 services/hotword_service.py）。K/V 操作经 hotword_kv_io 的 _WriteLog，
抓取经 scrape_serp / scrape_trend 的模块引用。

★源级不可用（连不上 / 被挑战页占满）原样抛 SourceUnavailable，由 worker 层的
  _analyze_one 只让本词跳过该源，不中止整批。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from src.app.tools.adapters.cloudflare.kv import (
    CloudflareKV, term_key,
    SERP_PREFIX, TREND_PREFIX, COMPETITION_KEY, INTERESTS_KEY,
)
from .hotword_kv_io import _WriteLog, _kv_dict

# 模块引用（不是 from ... import fetch_*）：测试 monkeypatch.setattr(hw.scrape_serp, ...)
# patch 的是模块对象属性，所有共享该对象的地方都会生效。
from . import scrape_serp, scrape_trend
from .source_errors import SourceUnavailable

log = logging.getLogger(__name__)

SERP_GAP_SEC = 1.5          # 全局串行 + 最小间隔；202 的根因是请求头，见 scrape_serp.py
SERP_TTL = 604800           # serp:v1: 缓存 7 天
TREND_TTL = 259200          # trend:v1: 缓存 3 天

_serp_lock = threading.Lock()
_last_serp_at = 0.0


def _throttled_serp(term: str, kv: CloudflareKV,
                    writes: Optional[_WriteLog] = None,
                    diag: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any] | None, str]:
    """KV 已有 serp:v1: 直接读；缺失则现抓并写回。全局串行避免并发同时打 DDG。

    ★源级不可用（连不上 / 被挑战页占满）原样抛 SourceUnavailable，由 _analyze_one
      **只让本词跳过 SERP**，不再中止整批；其它异常降级为「无 SERP」，不拦 AI。
    ★diag 是给 LangSmith span 的现场记录（是否命中缓存、4 发尝试明细、抓取异常），
      不改变返回值形状，老调用方不受影响。
    """
    diag = {} if diag is None else diag
    diag["fromCache"] = False
    key = term_key(term)
    writes = writes or _WriteLog(term)
    cached = kv.get_json(SERP_PREFIX + key)
    if cached:
        diag["fromCache"] = True
        return cached, ""
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
        diag["fetchError"] = str(exc)[:300]
        return None, str(exc)[:120]
    diag["engine"] = s.get("engine")
    diag["attempts"] = s.get("attempts") or []
    comp = scrape_serp.score_competition(s["items"], term)
    payload = {"engine": s["engine"], "items": s["items"], "competition": comp, "at": int(time.time() * 1000)}
    # 单词缓存键：各词独立、无 read-modify-write，但仍要检查写返回值
    writes.json(kv, SERP_PREFIX + key, payload, ttl=SERP_TTL)
    # competition 是整表：读-改-写必须走原子更新，否则 4 个 worker 会互相覆盖
    entry = {"score": comp["score"], "level": comp["level"], "at": payload["at"]}
    writes.table(kv, COMPETITION_KEY, lambda m: {**m, key: entry}, reader=_kv_dict)
    return payload, ""


def _trend(term: str, kv: CloudflareKV,
           writes: Optional[_WriteLog] = None,
           diag: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
    """KV 已有 trend:v1: 直接读；缺失则现抓并写回 + 更新 interests 摘要表。

    ★源级不可用会抛 SourceUnavailable，由 _analyze_one 把「本批禁用趋势」置位——趋势只是
      辅助证据，不该因为它连不上就让整批分析白跑，但也不该每个词都去撞一遍超时。
    ★diag 同理记录命中缓存 / 抓取点数 / 尝试明细，供 tool:trend span 输出。
    """
    diag = {} if diag is None else diag
    diag["fromCache"] = False
    key = term_key(term)
    writes = writes or _WriteLog(term)
    cached = kv.get_json(TREND_PREFIX + key)
    if cached:
        diag["fromCache"] = True
        diag["pointCount"] = len(cached.get("points") or [])
        return True, ""
    try:
        t = scrape_trend.fetch_interest(term)
    except SourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 单词解析问题不影响结论
        log.warning("TREND 失败 %s: %s", term, exc)
        diag["fetchError"] = str(exc)[:300]
        return False, str(exc)[:120]
    diag["attempts"] = t.get("attempts") or []
    diag["pointCount"] = len(t.get("points") or [])
    diag["summary"] = t.get("summary") or {}
    payload = {"points": t["points"], "summary": t["summary"], "at": int(time.time() * 1000)}
    writes.json(kv, TREND_PREFIX + key, payload, ttl=TREND_TTL)
    entry = {
        "term": term, "summary": t["summary"], "at": payload["at"],
        "points": [p["v"] for p in t["points"]],
    }
    writes.table(kv, INTERESTS_KEY, lambda m: {**m, key: entry}, reader=_kv_dict)
    return True, ""
