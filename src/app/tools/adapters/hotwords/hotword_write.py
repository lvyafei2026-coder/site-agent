# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的结论回写层：一个词的正文 / 信号标签 / 跟进状态写回共享 KV。

与 hotword_fetch（补数据）对称，这里只管「分析完了怎么落库」。表形状与去重口径在
curation（upsert_saved / upsert_rejected / set_hint_override），键名契约在
../cloudflare/kv.py，写入成败记录在 hotword_kv_io 的 _WriteLog。

★正文先写：正文写失败就收手，绝不写状态——否则留下「有状态、没正文」的悬空词，
  select_pending 既不会重跑它，前端也看不到方案。
★「观望」与解析不出结论：只留正文和标签，状态保持未跟进（下次还会被取到）。
★本层不打 tool span：span 由 tools/registry.py 的统一 tracer 建
  （tool:write_hotword_result）；写入明细与失败清单随结果返回，由编排层汇总进该词 error。
"""

from __future__ import annotations

from typing import Any, Optional

from src.app.enums import CurationStatus, RejectSource, Verdict
from src.app.tools.adapters.cloudflare.kv import (
    get_kv, term_key, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, HINTS_KEY,
)
from .curation import set_hint_override, upsert_rejected, upsert_saved
from .hotword_kv_io import _WriteLog, _kv_dict, _kv_list

# 判定「做」时从候选条目带进 saved 表的字段（与 console 人工收藏同一套形状）
_SAVED_CANDIDATE_FIELDS = ("term", "related", "hints", "geos", "geoCount",
                           "traffic", "count", "score")


def write_result(item: dict[str, Any], text: str, hints: Optional[list[str]],
                 verdict: str, reason: str = "") -> dict[str, Any]:
    """把本词结论写回 KV，返回 {status, stoppedAfterTextFailure, textChars,
    textPreview, writes, writeErrors}。

    status 是本词的跟进结果：rejected（不做）/ todo（做）/ ""（观望或解析不出结论）。
    写入失败一律进 writeErrors，不抛异常——单词级失败不该拖垮整轮。
    """
    kv = get_kv()
    term = str(item.get("term") or "").strip()
    key = term_key(item.get("key") or item.get("term") or "")
    writes = _WriteLog(term)
    out: dict[str, Any] = {"status": "", "stoppedAfterTextFailure": False,
                           "textChars": len(text), "textPreview": text[:200],
                           "writes": [], "writeErrors": []}

    if not writes.text(kv, AI_CACHE_PREFIX + key, text):
        out["stoppedAfterTextFailure"] = True
    else:
        if hints is not None:
            writes.table(kv, HINTS_KEY,
                         lambda m: set_hint_override(m, term, hints), reader=_kv_dict)
        if verdict == Verdict.SKIP.value:
            payload = {"term": term, "reason": reason or "AI 判定不做",
                       "source": RejectSource.AI.value}
            if writes.table(kv, REJECTED_KEY,
                            lambda arr: upsert_rejected(arr, payload), reader=_kv_list):
                out["status"] = "rejected"
        elif verdict == Verdict.DO.value:
            cand = {k: item.get(k) for k in _SAVED_CANDIDATE_FIELDS}
            payload = {**cand, "term": term, "status": CurationStatus.TODO.value,
                       "verdict": Verdict.DO.value}
            if writes.table(kv, SAVED_KEY,
                            lambda arr: upsert_saved(arr, payload), reader=_kv_list):
                out["status"] = CurationStatus.TODO.value

    out["writes"] = writes.records
    out["writeErrors"] = writes.failed
    return out