# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线的 KV 写入层：写入账本 WriteLog + 一个词的分析结论落库。

谁用它：
    fetch.py                   取数回写 serp / trend 缓存与摘要表时，用 WriteLog 记账。
    tools/hotwords_tools.py    write_hotword_result 工具直接调 write_result。
    nodes/hotword_*_node.py    从工具结果里拿 writes / writeErrors，跨步骤汇总进该词 error。

表形状与去重口径在 wordlib（upsert_saved / upsert_rejected / set_hint_override），键名契约在
../cloudflare/kv.py；形状兜底读取与业务无关，在 src/app/utils/kv_shape.py。

★账本存在的理由：以前 `kv.put_json(...)` 的返回值被丢掉，Cloudflare 返回 401/5xx 时进程内
  仍以为写成功了，于是「正文有了、状态表没有」，而 select_pending 又会因为正文存在把这个词
  永久剔除。现在每次写都检查，失败清单随工具结果返回，进 items[].error。
★正文先写：正文写失败就收手，绝不写状态——否则留下「有状态、没正文」的悬空词，
  select_pending 既不会重跑它，前端也看不到方案。
★「观望」与解析不出结论：只留正文和标签，状态保持未跟进（下次还会被取到）。
★本层不打 tool span：span 由 tools/registry.py 的统一 tracer 建（tool:fetch_serp /
  tool:fetch_trend / tool:write_hotword_result）；写入明细与失败清单随结果返回，由编排层
  汇总进该词 error。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.app.enums import AnalysisPhase, CurationStatus, RejectSource, Verdict
from src.app.tools.adapters.cloudflare.kv import (
    atomic_update, get_kv, term_key, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, HINTS_KEY,
)
from src.app.utils.kv_shape import kv_dict, kv_list
from .wordlib import set_hint_override, upsert_rejected, upsert_saved

log = logging.getLogger(__name__)


class WriteLog:
    """单词级写入账本：这个词的每一次 put_* 都记结果，失败则汇总成 error。

    records 里的明细（key/kind/phase/ok）随工具结果返回，汇入 tool:fetch_serp /
    tool:fetch_trend / tool:write_hotword_result 的 outputs；写入体积不在账本里算，
    Trace 层按需裁剪。一个工具一本账（phase 固定），失败清单由编排层跨工具汇总。
    """

    def __init__(self, term: str = "") -> None:
        self.term = term
        self.failed: list[str] = []
        self.records: list[dict[str, Any]] = []
        self.phase = AnalysisPhase.ANALYSIS.value   # serp / trend / analysis

    def _record(self, key: str, ok: bool, kind: str) -> bool:
        rec: dict[str, Any] = {"key": key, "kind": kind,
                               "phase": self.phase, "ok": bool(ok)}
        self.records.append(rec)
        if not ok:
            self.failed.append(key)
            log.warning("KV 写入失败 term=%s key=%s", self.term, key)
        return ok

    def text(self, kv: Any, key: str, value: str, ttl: Optional[int] = None) -> bool:
        try:
            ok = bool(kv.put_text(key, value, ttl=ttl))
        except Exception as exc:  # noqa: BLE001 - 单词级写失败不拖垮其它词
            log.warning("KV 写入异常 term=%s key=%s: %s", self.term, key, exc)
            ok = False
        return self._record(key, ok, "put_text")

    def json(self, kv: Any, key: str, obj: Any, ttl: Optional[int] = None) -> bool:
        try:
            ok = bool(kv.put_json(key, obj, ttl=ttl))
        except Exception as exc:  # noqa: BLE001
            log.warning("KV 写入异常 term=%s key=%s: %s", self.term, key, exc)
            ok = False
        return self._record(key, ok, "put_json")

    def table(self, kv: Any, key: str, mutator, *, reader, ttl: Optional[int] = None) -> bool:
        ok, _updated = atomic_update(kv, key, mutator, reader=reader, ttl=ttl)
        return self._record(key, ok, "atomic_update")

    def error(self) -> str:
        return "KV 写入失败：" + "、".join(self.failed) if self.failed else ""


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
    writes = WriteLog(term)
    out: dict[str, Any] = {"status": "", "stoppedAfterTextFailure": False,
                           "textChars": len(text), "textPreview": text[:200],
                           "writes": [], "writeErrors": []}

    if not writes.text(kv, AI_CACHE_PREFIX + key, text):
        out["stoppedAfterTextFailure"] = True
    else:
        if hints is not None:
            writes.table(kv, HINTS_KEY,
                         lambda m: set_hint_override(m, term, hints), reader=kv_dict)
        if verdict == Verdict.SKIP.value:
            payload = {"term": term, "reason": reason or "AI 判定不做",
                       "source": RejectSource.AI.value}
            if writes.table(kv, REJECTED_KEY,
                            lambda arr: upsert_rejected(arr, payload), reader=kv_list):
                out["status"] = "rejected"
        elif verdict == Verdict.DO.value:
            cand = {k: item.get(k) for k in _SAVED_CANDIDATE_FIELDS}
            payload = {**cand, "term": term, "status": CurationStatus.TODO.value,
                       "verdict": Verdict.DO.value}
            if writes.table(kv, SAVED_KEY,
                            lambda arr: upsert_saved(arr, payload), reader=kv_list):
                out["status"] = CurationStatus.TODO.value

    out["writes"] = writes.records
    out["writeErrors"] = writes.failed
    return out
