# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""状态回填：把「正文已写、状态表丢了」的词补回去（否则永远不会被重跑，也看不见）。

拆分自 hotwords.py（2026-11-17）。**不调 LLM、不重抓 SERP/Trends**——重解析已落库
正文的结论与信号标签，按同一套 upsert_* / set_hint_override 口径补写状态。

触发场景：正文写成功、但 5 张整表里某次 put_json 失败（或历史上并发 lost update），
于是 `_select_pending` 因「已有正文」永久剔除该词，saved/rejected 里却查不到它——
既不会重跑、前端也看不见。
"""

from __future__ import annotations

import logging
from typing import Any

from src.app.enums import BackfillAction, CurationStatus, RejectSource, Verdict

from src.app.tools.adapters.cloudflare.kv import (
    get_kv, term_key, table_lock,
    AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY, HINTS_KEY,
)
from .curation import upsert_saved, upsert_rejected, set_hint_override
from .hotword_criteria import decide, signals_of
from .hotword_kv_io import _WriteLog, _kv_list, _kv_dict

log = logging.getLogger(__name__)


def _curated_keys(saved: list, rejected: list, tags: dict) -> set[str]:
    """已整理的词 key 集合：saved / rejected / tags 三处状态合并。

    扫描开始时用读到的快照判断缺口；写入锁内再以实时 KV 复核一次——回填期间
    可能正好有一批分析写完状态，别覆盖人工/新结论。
    """
    keys: set[str] = set()
    for row in saved + rejected:
        if isinstance(row, dict) and row.get("key"):
            keys.add(term_key(row["key"]))
    keys.update(term_key(k) for k, v in tags.items() if v)
    return keys


def backfill_states(*, limit: int = 200, dry_run: bool = False) -> dict[str, Any]:
    """从已落库的 `ai:v3:<key>` 正文反推结论，补写缺失的 saved / rejected / aihints。

    dry_run=True 只出报告不写，建议先跑一次核对。结论「观望」按设计不写状态（只补标签）；
    正文为空 / 结论解析不出且状态缺失的词会列进 items（action=unresolved）——要重跑它们
    得先删掉对应的 `ai:v3:` 正文，因为 _select_pending 只认正文存在与否。
    """
    kv = get_kv()
    limit = max(1, min(int(limit or 200), 1000))
    report: dict[str, Any] = {
        "ok": True, "dryRun": bool(dry_run), "scanned": 0, "repaired": 0,
        "hintsRepaired": 0, "alreadyCurated": 0, "unresolved": 0,
        "writeErrors": [], "items": [],
    }
    try:
        cache_keys = [k for k in kv.list_keys(AI_CACHE_PREFIX)
                      if k.startswith(AI_CACHE_PREFIX)]
        saved = _kv_list(kv, SAVED_KEY)
        rejected = _kv_list(kv, REJECTED_KEY)
        tags = _kv_dict(kv, TAGS_KEY)
        hints_map = _kv_dict(kv, HINTS_KEY)
        cand_map: dict[str, dict[str, Any]] = {}
        for c in _kv_list(kv, "candidates"):
            if isinstance(c, dict) and c.get("term"):
                cand_map[term_key(c.get("key") or c["term"])] = c
    except Exception as exc:  # noqa: BLE001
        log.warning("状态回填读取 KV 失败：%s", exc)
        report["ok"] = False
        report["error"] = "状态回填读取 KV 失败：" + str(exc)[:200]
        return report

    curated = _curated_keys(saved, rejected, tags)

    for cache_key in cache_keys[:limit]:
        key = cache_key[len(AI_CACHE_PREFIX):]
        if not key:
            continue
        report["scanned"] += 1
        text = kv.get_text(cache_key) or ""
        if not text.strip():
            report["unresolved"] += 1
            report["items"].append({"key": key, "action": BackfillAction.UNRESOLVED.value,
                                    "reason": "正文为空"})
            continue

        verdict, reason = decide(text)
        hints = signals_of(text)
        need_state = key not in curated and verdict in (Verdict.DO.value, Verdict.SKIP.value)
        # 「信号标签：无」解析成 []，写成空覆盖没有意义，回填时跳过
        need_hints = bool(hints) and key not in hints_map

        if not need_state and not need_hints:
            # 结论「观望」本来就没有状态，和「已整理」一样不算缺口；
            # 只有结论解析不出、状态也没补上的才算真卡住。
            if verdict:
                report["alreadyCurated"] += 1
            else:
                report["unresolved"] += 1
                report["items"].append({
                    "key": key, "action": BackfillAction.UNRESOLVED.value,
                    "reason": "正文解析不出结论且状态缺失；要重跑需先删除该词的 ai:v3: 正文",
                })
            continue

        action = (BackfillAction.REPAIRED.value if not dry_run
                  else BackfillAction.PREVIEW.value)
        entry: dict[str, Any] = {
            "key": key, "action": action, "verdict": verdict or Verdict.UNDECIDED.value,
            "state": verdict if need_state else "", "hints": bool(need_hints),
        }
        report["items"].append(entry)
        if dry_run:
            continue

        writes = _WriteLog(key)
        cand = cand_map.get(key, {})
        # 与正在跑的 batch 共用同一把 RLock（可重入）：状态 + 标签一起改完再放开，
        # 避免回填到一半被另一个词插进来读到半套数据。
        with table_lock():
            if need_state and key in _curated_keys(
                    _kv_list(kv, SAVED_KEY), _kv_list(kv, REJECTED_KEY),
                    _kv_dict(kv, TAGS_KEY)):
                # 回填扫描期间刚被整理过 → 不再覆盖，保证幂等、不抢人工标记
                need_state = False
                report["alreadyCurated"] += 1
                entry["state"] = ""
            need_hints = bool(hints) and key not in _kv_dict(kv, HINTS_KEY)
            entry["hints"] = bool(need_hints)
            if not need_state and not need_hints:
                entry["action"] = BackfillAction.SKIPPED.value
            if need_state and verdict == Verdict.SKIP.value:
                payload = {"term": cand.get("term") or key,
                           "reason": reason or "AI 判定不做", "source": RejectSource.AI.value}
                if writes.table(kv, REJECTED_KEY,
                                lambda arr: upsert_rejected(arr, payload), reader=_kv_list):
                    report["repaired"] += 1
            elif need_state and verdict == Verdict.DO.value:
                fields = ("term", "related", "hints", "geos", "geoCount", "traffic", "count", "score")
                payload = {**{f: cand.get(f) for f in fields},
                           "term": cand.get("term") or key,
                           "status": CurationStatus.TODO.value, "verdict": Verdict.DO.value}
                if writes.table(kv, SAVED_KEY,
                                lambda arr: upsert_saved(arr, payload), reader=_kv_list):
                    report["repaired"] += 1
            if need_hints:
                if writes.table(kv, HINTS_KEY,
                                lambda m: set_hint_override(m, key, hints), reader=_kv_dict):
                    report["hintsRepaired"] += 1
        report["writeErrors"].extend(writes.failed)

    if report["writeErrors"]:
        report["ok"] = False
        report["error"] = "部分状态回填写入失败：" + "、".join(dict.fromkeys(report["writeErrors"]))
    return report
