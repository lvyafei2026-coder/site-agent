# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""热词子系统的**共享契约叶子**：候选筛选 + 判例读取 + 逐词结果形状 + 成功阈值。

依赖方向上的最底层：只依赖 cloudflare.kv、hotword_kv_io 与纯判据 hotword_criteria，
**不 import graph / nodes / services / tools 契约中任何一个**。

谁用它：
    tools/hotwords_tools.py        next_candidate / read_examples 两个工具的实现。
    nodes/hotword_*_node.py        步骤节点填同一条词结果（new_word / add_notice /
                                   add_write_errors / finish_word）。
    nodes/hotword_summary_node.py  汇总节点：成功阈值。
"""

from __future__ import annotations

from typing import Any

from src.app.tools.adapters.cloudflare.kv import (
    get_kv, term_key, AI_CACHE_PREFIX, SAVED_KEY, REJECTED_KEY, TAGS_KEY,
)
from .hotword_criteria import pick_examples
from .hotword_kv_io import _kv_list, _kv_dict

OK_MIN_RATE = 0.5                    # 整轮成功判定：成功率低于此值即视为失败


def new_word(term: str) -> dict[str, Any]:
    """一条词的**结果骨架**：逐词子图里的步骤节点各自往里填，collect_word 落进 results。

    形状只在这里定义——前端逐词表与汇总都读这几个键（evidence 的键 = 本轮走过的步骤名）。
    """
    return {"term": term, "verdict": "", "status": "", "model": "",
            "evidence": {}, "error": ""}


def add_notice(word: dict[str, Any], text: str) -> None:
    """「没拿到数据但不算失败」的信息按步骤顺序串起来（源级降级、缓存里也没结果）。"""
    word["notice"] = (f"{word['notice']}；{text}" if word.get("notice") else text)


def add_error(word: dict[str, Any], text: str) -> None:
    """单词级失败：LLM 挂 / 后置动作挂，只落在这一条词上。"""
    word["error"] = (f"{word['error']}；{text}" if word["error"] else text)


def add_write_errors(word: dict[str, Any], keys: list[str]) -> None:
    """跨步骤汇总写入失败明细（各工具自报 result.writeErrors）。"""
    if keys:
        word.setdefault("writeErrors", []).extend(keys)


def finish_word(word: dict[str, Any]) -> dict[str, Any]:
    """落账前的收尾：有任何一个 KV 写失败就不算 analyzed。"""
    write_errors = word.get("writeErrors") or []
    if write_errors:
        add_error(word, "KV 写入失败：" + "、".join(write_errors))
    return word


def read_examples() -> list[dict[str, str]]:
    """读人工「不做」标记并挑出可喂回 AI 的判例（挑选规则在纯判据层）。"""
    return pick_examples(_kv_list(get_kv(), REJECTED_KEY))


def select_pending(seen: set[str]) -> dict[str, Any] | None:
    """取「下一个」未跟进的热词；没有就返回 None。

    每轮只取一条，由调用方（循环图的 fetch_one 节点经 next_candidate 工具）反复调用
    直到 None 或达上限。
    ★seen 是本轮已取过的 key：KV 最终一致，刚写的状态可能读不到，靠它在**本轮内**
      防止同一个词被重复取出。跨轮的幂等仍由 curated / analyzed 两张 KV 表兜底。

    ★形状兜底：KV 里这几张表是 console/radar 写的，字段形状一旦漂移（比如 tags 变成数组、
    candidates 变成对象），按原样 .items() / 迭代 会整轮失败。这里统一按「期望类型不符就
    当空」处理并 warn，让漂移可见但不阻断分析。
    """
    kv = get_kv()
    candidates = _kv_list(kv, "candidates")
    saved = _kv_list(kv, SAVED_KEY)
    rejected = _kv_list(kv, REJECTED_KEY)
    tags = _kv_dict(kv, TAGS_KEY)

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
