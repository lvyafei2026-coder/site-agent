# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的 KV 写账本层：单词级写入成败记录 + 共享表形状兜底读取。

拆分自 hotwords.py（2026-11-17）。只放「怎么安全地写 / 怎么容错地读」这一层；
取数（_throttled_serp / _trend）在 hotword_fetch.py，单词分析 / 回填在
services/hotword_service.py / services/backfill_service.py，逐词图在 graph/hotword_graph.py。

★_WriteLog 存在的理由：以前 `kv.put_json(...)` 的返回值被丢掉，Cloudflare 返回
  401/5xx 时进程内仍以为写成功了，于是「正文有了、状态表没有」，而 select_pending
  又会因为正文存在把这个词永久剔除。现在每次写都检查，失败清单会进 items[].error。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.app.enums import AnalysisPhase

from src.app.tools.adapters.cloudflare.kv import CloudflareKV, atomic_update

log = logging.getLogger(__name__)


class _WriteLog:
    """单词级写入账本：这个词的每一次 put_* 都记结果，失败则汇总成 error。

    records 里的明细（key/kind/phase/ok）由切面汇入 tool:kv.write / tool:serp /
    tool:trend 的 outputs；写入体积不在账本里算，Trace 层按需裁剪。
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

    def records_for(self, phase: AnalysisPhase) -> list[dict[str, Any]]:
        """取某一阶段的写入明细，供切面挂到对应 tool span 的 outputs。"""
        return [r for r in self.records if r["phase"] == phase.value]

    def error(self) -> str:
        return "KV 写入失败：" + "、".join(self.failed) if self.failed else ""


def _kv_list(kv: CloudflareKV, key: str) -> list:
    """读一个「应该是数组」的 KV 键；形状不对当空并 warn，不抛。"""
    val = kv.get_json(key, [])
    if isinstance(val, list):
        return val
    if val is not None and val != []:
        log.warning("KV 键 %s 期望数组，实际 %s，按空处理", key, type(val).__name__)
    return []


def _kv_dict(kv: CloudflareKV, key: str) -> dict:
    """读一个「应该是对象」的 KV 键；形状不对当空并 warn，不抛。"""
    val = kv.get_json(key, {})
    if isinstance(val, dict):
        return val
    if val is not None and val != {}:
        log.warning("KV 键 %s 期望对象，实际 %s，按空处理", key, type(val).__name__)
    return {}
