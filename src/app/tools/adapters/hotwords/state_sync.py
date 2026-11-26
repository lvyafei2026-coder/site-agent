# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；详见 LICENSE.

"""热词跟进状态闭环：把 agent_pages 台账的建页进度投影回 saved 表。

状态映射（台账 → saved）：
    planned                         → wip   开发中（已进待建页，排产选中）
    generated / deployed / indexed / verified → live  已上线
    failed                          → 不动（建页失败，保留原跟进状态）

★只升级不降级：saved < todo < wip < live。已经 live 的词不退回 wip，
  parked（人工搁置）不自动改——那是人工决定，不替他推翻。
★不动没收藏过的词：台账里有但 saved 表没这条，说明它不是「待开发列表里的热词」，
  不在这里凭空新建收藏。
★台账 keyword 有两种形态：排产落库写原始词，git_push 落库写 slug。匹配前把
  连字符/下划线也折叠成空白，两边都过 term_key，否则 is-it-black-friday-today
  对不上 is it black friday today。

本模块是 adapters 叶子：只用 cloudflare 的 KV / D1 与 utils，不认识 tools / services。
写入侧（排产 / 建页节点）通过它即时同步，对账入口也复用同一套 project()。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from src.app.enums import CurationStatus, PageQueryMode, PageStatus
from src.app.tools.adapters.cloudflare import database
from src.app.tools.adapters.cloudflare.kv import (
    SAVED_KEY, atomic_update, get_kv, term_key,
)
from src.app.tools.adapters.cloudflare.sql import PAGE_QUERIES
from src.app.utils.kv_shape import kv_list

# 只升级不降级的次序；parked 不在表里 —— 人工搁置是明确决定，自动同步不碰它。
_RANK = {
    CurationStatus.SAVED.value: 0,
    CurationStatus.TODO.value: 1,
    CurationStatus.WIP.value: 2,
    CurationStatus.LIVE.value: 3,
}

# 台账状态 → 目标跟进状态；failed 与其它状态不投影。
_TARGET = {
    PageStatus.PLANNED.value: CurationStatus.WIP.value,
    PageStatus.GENERATED.value: CurationStatus.LIVE.value,
    PageStatus.DEPLOYED.value: CurationStatus.LIVE.value,
    PageStatus.INDEXED.value: CurationStatus.LIVE.value,
    PageStatus.VERIFIED.value: CurationStatus.LIVE.value,
}

_MAX_ROWS = 1000


def norm_term(term: Any) -> str:
    """台账 keyword / saved term → 同一个 key：小写 + 空白折叠 + 连字符归一化。"""
    return term_key(str(term or "").replace("-", " ").replace("_", " "))


def project(rows: list[dict], saved: list[dict]) -> tuple[list[dict], list[dict]]:
    """纯函数：算「台账投影后的 saved 表」与变更明细，不碰 IO。

    同一词有多条台账行（不同站/不同阶段）时取更靠后的状态；只升级已存在的收藏，
    不新增词。返回 (new_saved, changes)，changes 每项 {term, from, to, key}。
    """
    ts = int(time.time() * 1000)
    targets: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        key = norm_term(row.get("keyword"))
        target = _TARGET.get(str(row.get("status") or ""))
        if not key or not target:
            continue
        if _RANK[target] > _RANK.get(targets.get(key, ""), -1):
            targets[key] = target

    out: list[dict] = []
    changes: list[dict] = []
    for item in saved if isinstance(saved, list) else []:
        if not isinstance(item, dict):
            out.append(item)
            continue
        key = norm_term(item.get("term") or item.get("key"))
        target = targets.get(key)
        cur = str(item.get("status") or "")
        if target and cur != CurationStatus.PARKED.value and (
                _RANK[target] > _RANK.get(cur, -1)):
            item = {**item, "status": target, "updatedAt": ts}
            changes.append({"term": item.get("term") or "", "key": key,
                            "from": cur, "to": target})
        out.append(item)
    return out, changes


def read_ledger() -> list[dict]:
    """读 agent_pages 全量（排除 failed）；台账不可达时返回空，让对账降级。"""
    query = PAGE_QUERIES[PageQueryMode.ALL.value]
    try:
        rows = database.query(f"{query.sql} LIMIT ?", [_MAX_ROWS])
    except Exception:  # noqa: BLE001 - 对账失败不该打断调用方（排产/建页）
        return []
    return rows if isinstance(rows, list) else []


def sync(rows: Optional[list[dict]] = None) -> dict:
    """把台账状态同步回 saved 表（幂等，只写有变化的词）。

    rows=None 时自己读台账；调用方已经有台账行时可传入，避免重复读。
    读失败/写失败都返回 ok=False + 明细，不抛异常。
    """
    source = read_ledger() if rows is None else rows
    kv = get_kv()
    current = kv_list(kv, SAVED_KEY)
    _, pre_changes = project(source, current)
    if not pre_changes:
        return {"ok": True, "updated": 0, "changes": []}

    # ★并发安全：project 必须放进 mutator、基于锁内读到的最新 saved 重算，
    #   不能用锁外快照写死。否则写后读回发现被别的进程覆盖时，重试只会把同一份
    #   旧表再写回去，照样丢对方的行。changes 用闭包带出最后一次重算的明细。
    changes: list[dict] = []

    def mutator(fresh: list[dict]) -> list[dict]:
        new_saved, diff = project(source, fresh)
        changes[:] = diff
        return new_saved

    ok, _ = atomic_update(kv, SAVED_KEY, mutator, reader=kv_list)
    if not ok:
        return {"ok": False, "updated": 0, "changes": changes, "error": "KV 写入失败"}
    return {"ok": True, "updated": len(changes), "changes": changes}


def mark(terms: list[str], status: str) -> dict:
    """即时同步：把指定的词（若已收藏且状态落后）直接设成目标状态，不读台账。"""
    if status not in _RANK:
        return {"ok": False, "updated": 0, "changes": []}
    rows = [{"keyword": t, "status": _PAGE_STATUS_OF[status]} for t in terms]
    return sync(rows)


# mark() 的目标状态反查台账状态：让 mark 复用 project() 同一套升级规则。
_PAGE_STATUS_OF = {
    CurationStatus.WIP.value: PageStatus.PLANNED.value,
    CurationStatus.LIVE.value: PageStatus.DEPLOYED.value,
}
