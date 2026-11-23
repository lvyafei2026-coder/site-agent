# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""词库层：共享 KV 里那几张词表的读写口径（candidates / saved / rejected / hints）。

读侧（radar 把抓取结果写进同一个 KV，见 google-trend-radar/src/index.js runRefresh，
agent 直接读这份共享数据，不调 radar 的 App 接口）：
    snapshot        候选 + meta + interests 的完整快照。Snapshot 原样透出 meta
                    （updatedMs / geosOk / raw / failed）：判断「有没有新热词」看
                    meta.updatedMs，不能看 candidates 是否非空——累计词库几乎永远非空。
    select_pending  取下一个未跟进的词（saved / rejected / tags / ai:v3: 取并集后跳过）。
    read_examples   读人工「不做」标记，挑出可喂回 AI 的判例（挑选规则就在本文件的
                    pick_examples：rejected 表的形状与字段口径都归这层）。

写侧口径（与 console/src/store.js 原样对齐）：
    agent 直写共享 KV，必须和 console 人工操作落在同一套键、同一套状态枚举上——
    否则 agent 写的 saved 和人工点的收藏对不上。下面三个 upsert / set_* 是**纯函数**：
    传进现有表、返回新表，不碰网络（写回与逐词写入账本在 writeback.py，
    网络与键名契约在 ../cloudflare/kv.py，全项目唯一的 KV 模块）。

saved 表：[{key, term, status, at, updatedAt, ...}]，status ∈ STATUSES。
rejected 表：[{key, term, reason, source, at, updatedAt}]。
aihints：{ termKey: [信号名] }。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from src.app.enums import CandidateStatus, CurationStatus, HintName, RejectSource

from src.app.tools.adapters.cloudflare.kv import (
    AI_CACHE_PREFIX, CANDIDATES_KEY, INTERESTS_KEY, REJECTED_KEY, SAVED_KEY,
    TAGS_KEY, CloudflareKV, get_kv, term_key,
)
from src.app.utils.kv_shape import kv_dict, kv_list

log = logging.getLogger(__name__)

# 与 console/src/store.js 完全一致；todo = Agent 分析后判定「可以做」自动置入
STATUSES = [s.value for s in CurationStatus]

SAVED_FIELDS = ["term", "related", "hints", "geos", "geoCount", "traffic", "count", "score", "verdict"]
HINT_NAMES = [h.value for h in HintName]

# 判例反馈条数上限：从 rejected 表挑少量真实词例喂回 AI，让它对同类词倾向判不做。
# ★v2 提示词已固化高频规则（非工具意图/时效资讯/热度下降/真人/作品），这里只取
#   「人工判定 + 有泛化价值理由」的词，避开已固化的占位理由，防止提示词与判例重复。
EXAMPLE_LIMIT = 8
_EXAMPLE_SKIP_RX = re.compile(
    r"搜索意图不是找工具|热度下降|趋势下降|未填原因|旧版本本地标记"
)


# ---- 读侧：候选池快照 ----

def _read(kv: CloudflareKV, status: str, limit: int, light: bool) -> dict[str, Any]:
    # ★这三张表由 radar / console 写，形状漂移时按空处理，不能让整次工具调用失败
    meta = kv_dict(kv, "meta")
    interests = {} if light else kv_dict(kv, INTERESTS_KEY)

    # 跟进状态（todo / wip / live / parked）只存在 saved 表，candidates 条目本身
    # 不带 status。word_create_page 取「待开发」时直接回 saved 过滤，查不到就给空——
    # 绝不能回落成全量候选，否则没分析过的词会混进排产视野。
    if status != CandidateStatus.DOING.value:
        rows = [r for r in kv_list(kv, SAVED_KEY)
                if isinstance(r, dict) and r.get("status") == status]
        return {"items": rows[:limit or None], "meta": meta, "interests": interests}

    candidates = kv_list(kv, CANDIDATES_KEY)
    items = candidates if not limit else candidates[:limit]
    # 候选条目一般不带 status 字段（status 在 saved/rejected/tags 里），按原 radar.py 语义
    # 默认都算 doing；老条目没有 status 时不能全丢。
    if status:
        filtered = [i for i in items if isinstance(i, dict)
                    and i.get("status", CandidateStatus.DOING.value) == status]
        items = filtered or items

    return {"items": items, "meta": meta, "interests": interests}


def snapshot(status: str = CandidateStatus.DOING.value, limit: int = 40,
             light: bool = False) -> dict[str, Any]:
    """给 Agent 的完整快照：候选 + meta + interests。
    status=doing（默认）读 candidates 未跟进词；其他状态（todo 等）读 saved 表过滤——
    跟进状态只存在 saved，candidates 条目不带 status。"""
    kv = get_kv()
    try:
        return _read(kv, status, limit, light)
    except Exception as exc:  # noqa: BLE001
        log.warning("candidates 读取失败：%s", exc)
        return {"items": [], "meta": {}, "error": str(exc), "interests": {}}


# ---- 读侧：取下一词 / 读判例 ----

def read_examples() -> list[dict[str, str]]:
    """读人工「不做」标记并挑出可喂回 AI 的判例。"""
    return pick_examples(kv_list(get_kv(), REJECTED_KEY))


def pick_examples(rejected: list, limit: int = EXAMPLE_LIMIT) -> list[dict[str, str]]:
    """从 rejected 表挑判例：只人工标记、理由有泛化价值、按理由去重。

    - 排除 `source=ai`（AI 自己的判定不是「学习素材」）与无/占位理由；
    - 理由过短（<4 字，多是「世界末日」这类纯释义）或无信息量则跳过；
    - 同一理由只留最新一条，避免 8 条「搜索意图不是找工具」刷屏；
    - 按 updatedAt/at 倒序取最近 ``limit`` 条。
    """
    if not isinstance(rejected, list):
        return []
    seen_reason: set[str] = set()
    out: list[dict[str, str]] = []
    # 先按时间倒序（最新在前），同理由去重时自然保留最新一条。
    for row in sorted(
        (x for x in rejected if isinstance(x, dict)),
        key=lambda x: (x.get("updatedAt") or x.get("at") or 0),
        reverse=True,
    ):
        if row.get("source") != "manual":
            continue
        term = str(row.get("term") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if not term or len(reason) < 4 or _EXAMPLE_SKIP_RX.search(reason):
            continue
        # 理由归一化去重：去掉首尾标点与空白，同类理由只留一条。
        norm = re.sub(r"[\s，。；、,.;:：]+$", "", reason)
        if norm in seen_reason:
            continue
        seen_reason.add(norm)
        out.append({"term": term[:60], "reason": reason[:120]})
        if len(out) >= max(0, min(int(limit), 20)):
            break
    return out


def select_pending(seen: set[str]) -> dict[str, Any] | None:
    """取「下一个」未跟进的热词；没有就返回 None。

    每轮只取一条，由调用方（循环图的 fetch_one 节点经 next_candidate 工具）反复调用
    直到 None 或达上限。
    ★seen 是本轮已取过的 key：KV 最终一致，刚写的状态可能读不到，靠它在**本轮内**
      防止同一个词被重复取出。跨轮的幂等仍由 curated / analyzed 两张 KV 表兜底。

    ★形状兜底：这几张表由 console / radar 写，一旦漂移（比如 tags 变成数组、
      candidates 变成对象）按原样迭代会整轮失败；kv_shape 的 kv_list / kv_dict
      统一当成空处理
      并 warn，让漂移可见但不阻断分析。
    """
    kv = get_kv()
    candidates = kv_list(kv, CANDIDATES_KEY)
    saved = kv_list(kv, SAVED_KEY)
    rejected = kv_list(kv, REJECTED_KEY)
    tags = kv_dict(kv, TAGS_KEY)

    curated = set()
    for x in (saved + rejected):
        if isinstance(x, dict) and x.get("key"):
            curated.add(term_key(x["key"]))
    for k, v in tags.items():
        if v:
            curated.add(term_key(k))

    analyzed = {k[len(AI_CACHE_PREFIX):] for k in kv.list_keys(AI_CACHE_PREFIX)}

    for c in candidates:
        if not isinstance(c, dict) or not c.get("term"):
            continue
        key = term_key(c.get("key") or c.get("term"))
        if key in curated or key in analyzed or key in seen:
            continue
        return {
            "key": key,
            "term": str(c.get("term") or "")[:120],
            "related": [str(x) for x in (c.get("related") or [])][:6],
            "hints": [str(x) for x in (c.get("hints") or [])][:6],
            "geos": [str(x) for x in (c.get("geos") or [])][:16],
            "geoCount": int(c.get("geoCount") or (len(c.get("geos") or []))),
            "traffic": int(c.get("traffic") or 0),
            "count": int(c.get("count") or 1),
            "score": int(c.get("score") or 0),
            "src": str(c.get("src") or "trends"),
        }
    return None


# ---- 写侧：saved / rejected / aihints ----

def _pick_saved(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in SAVED_FIELDS:
        v = item.get(f)
        if v is not None:
            out[f] = v
    if isinstance(out.get("related"), list):
        out["related"] = [str(x) for x in out["related"][:6]]
    if isinstance(out.get("hints"), list):
        out["hints"] = [str(x) for x in out["hints"][:6]]
    if isinstance(out.get("geos"), list):
        out["geos"] = [str(x) for x in out["geos"][:16]]
    if isinstance(out.get("term"), str):
        out["term"] = out["term"][:120]
    if isinstance(out.get("verdict"), str):
        out["verdict"] = out["verdict"][:12]
    return out


def upsert_saved(list_: Optional[list], item: dict[str, Any]) -> list:
    """新增或更新一条收藏；同词去重，首次收藏时间不变，更新 updatedAt。

    status 只认 STATUSES 里的；传错或缺失时保留原状态，首次默认 saved。
    """
    now = int(time.time() * 1000)
    key = term_key(item.get("term"))
    if not key:
        return list_ if isinstance(list_, list) else []
    arr = [x for x in (list_ or []) if isinstance(x, dict) and x.get("key") != key]
    prev = next((x for x in (list_ or []) if isinstance(x, dict) and x.get("key") == key), None)
    status = item.get("status")
    if status not in STATUSES:
        status = (prev.get("status") if prev else CurationStatus.SAVED.value)
    merged = {**_pick_saved(prev or {}), **_pick_saved(item), "key": key, "status": status}
    merged["at"] = (prev.get("at") if prev else now)
    merged["updatedAt"] = now
    arr.append(merged)
    arr.sort(key=lambda x: x.get("at", 0), reverse=True)
    return arr


def upsert_rejected(list_: Optional[list], item: dict[str, Any]) -> list:
    """标记「不做」；人工标记优先：AI 重复判定不改原因、不降级来源。"""
    now = int(time.time() * 1000)
    key = term_key(item.get("term"))
    if not key:
        return list_ if isinstance(list_, list) else []
    arr = [x for x in (list_ or []) if isinstance(x, dict) and x.get("key") != key]
    prev = next((x for x in (list_ or []) if isinstance(x, dict) and x.get("key") == key), None)
    incoming = str(item.get("reason") or "").strip()[:300]
    source = RejectSource.AI.value if item.get("source") == RejectSource.AI.value else RejectSource.MANUAL.value
    keep_manual = bool(prev and prev.get("source") == RejectSource.MANUAL.value and source == RejectSource.AI.value)
    reason = (str(prev.get("reason") or "")[:300] if keep_manual else (incoming or (prev.get("reason") or "")))
    arr.append({
        "key": key,
        "term": str(item.get("term") or (prev and prev.get("term")) or "").strip()[:120],
        "reason": reason,
        "source": RejectSource.MANUAL.value if keep_manual else source,
        "at": (prev.get("at") if prev else now),
        "updatedAt": now,
    })
    arr.sort(key=lambda x: x.get("at", 0), reverse=True)
    return arr


def set_hint_override(map_: Optional[dict], term: str, hints: list) -> dict:
    """覆盖某词的信号标签；只保留 HINT_NAMES 内的、最多 3 个。"""
    out = dict(map_ or {})
    key = term_key(term)
    if not key:
        return out
    out[key] = [
        str(h) for h in (hints or [])
        if str(h) in HINT_NAMES
    ][:3]
    return out
