# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线的 KV 写入账本：记录一个词的每一次写入成败（业务口径，带阶段名）。

谁用它：fetch.py（serp / trend 的缓存与摘要表回写）与 writeback.py（正文 / 标签 /
状态落库）。一个工具一本账，失败清单随工具结果返回，由逐词节点跨工具汇总进该词的 error。

★存在的理由：以前 `kv.put_json(...)` 的返回值被丢掉，Cloudflare 返回 401/5xx 时进程内
  仍以为写成功了，于是「正文有了、状态表没有」，而 select_pending 又会因为正文存在把
  这个词永久剔除。现在每次写都检查，失败清单会进 items[].error。

形状兜底读取（kv_list / kv_dict）不在这一层——那是与业务无关的读侧容错，在
src/app/utils/kv_shape.py。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.app.enums import AnalysisPhase

from src.app.tools.adapters.cloudflare.kv import atomic_update

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
