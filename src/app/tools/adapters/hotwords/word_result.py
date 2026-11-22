# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""一条词的**结果形状**：逐词子图各步骤往里填，收尾时算成功与否（纯内存，无 IO）。

谁用它：
    nodes/hotword_*_node.py        步骤节点填同一条词结果（new_word / add_notice /
                                   add_error / add_write_errors / finish_word）。
    nodes/hotword_summary_node.py  汇总节点：成功阈值 OK_MIN_RATE。

取词与读判例不在这里——那是词表的读，在 wordlib.py。本模块只认这个 dict：形状只在
new_word 一处定义，前端逐词表与汇总读的就是这几个键（evidence 的键 = 本轮走过的步骤名）。
"""

from __future__ import annotations

from typing import Any

OK_MIN_RATE = 0.5                    # 整轮成功判定：成功率低于此值即视为失败


def new_word(term: str) -> dict[str, Any]:
    """一条词的**结果骨架**：逐词子图里的步骤节点各自往里填，collect_word 落进 results。"""
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

