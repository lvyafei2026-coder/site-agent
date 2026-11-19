# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的 KV 写账本层：单词级写入成败记录 + 共享表形状兜底读取。

拆分自 hotwords.py（2026-11-17）。只放「怎么安全地写 / 怎么容错地读」这一层；
取数（_throttled_serp / _trend）在 hotword_fetch.py，单词/批次编排在
hotword_worker.py / hotword_batch.py（2026-11-19 拆）。

★_WriteLog 存在的理由：以前 `kv.put_json(...)` 的返回值被丢掉，Cloudflare 返回
  401/5xx 时进程内仍以为写成功了，于是「正文有了、状态表没有」，而 _select_pending
  又会因为正文存在把这个词永久剔除。现在每次写都检查，失败清单会进 items[].error。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.app.enums import AnalysisPhase

from src.app.tools.adapters.cloudflare.kv import CloudflareKV, atomic_update

log = logging.getLogger(__name__)


class _WriteLog:
    """单词级写入账本：这个词的每一次 put_* 都记结果，失败则汇总成 error。

    records 里的明细（key/kind/phase/ok）供 LangSmith 的 tool:kv.write /
    tool:serp / tool:trend 输出；关闭追踪时只记四个小字段，不算体积、零额外开销。
    """

    def __init__(self, term: str = "") -> None:
        self.term = term
        self.failed: list[str] = []
        self.records: list[dict[str, Any]] = []
        self.phase = AnalysisPhase.ANALYSIS.value   # serp / trend / analysis
        self.capture = False      # tracing 开启时才计算写入字节数

    def _record(self, key: str, ok: bool, kind: str, value: Any = None) -> bool:
        rec: dict[str, Any] = {"key": key, "kind": kind,
                               "phase": self.phase, "ok": bool(ok)}
        if self.capture:
            rec["bytes"] = _io_size(value)
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
        return self._record(key, ok, "put_text", value)

    def json(self, kv: Any, key: str, obj: Any, ttl: Optional[int] = None) -> bool:
        try:
            ok = bool(kv.put_json(key, obj, ttl=ttl))
        except Exception as exc:  # noqa: BLE001
            log.warning("KV 写入异常 term=%s key=%s: %s", self.term, key, exc)
            ok = False
        return self._record(key, ok, "put_json", obj)

    def table(self, kv: Any, key: str, mutator, *, reader, ttl: Optional[int] = None) -> bool:
        ok, updated = atomic_update(kv, key, mutator, reader=reader, ttl=ttl)
        return self._record(key, ok, "atomic_update", updated)

    def error(self) -> str:
        return "KV 写入失败：" + "、".join(self.failed) if self.failed else ""


def _io_size(value: Any) -> int:
    """只在 tracing 开启时调用：给 span 里的「写了多大」一个可解释的量级。"""
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return 0


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
