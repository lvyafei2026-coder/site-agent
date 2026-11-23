# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线的判定口径：SERP 证据降维、作品/红线硬证据、AI 正文里的结论与信号标签解析。

纯文本进、结论出，不碰 KV / LLM / 抓取：输入提示词在 prompts/hotword_messages.py，
外部取数在 tools/adapters/hotwords/（经 tools 层的工具调用），传输在 utils/llm_io.py。

谁用它：
    nodes/hotword_llm_node.py    decide（结论 + 红线压回）/ signals_of（第 8 节标签）。
    nodes/hotword_serp_node.py   serp_brief（喂 AI 的前 6 名）/ work_signal（作品硬证据）。
    判定解析的格式契约来自 D1 提示词的 8 小节，改动要同步 test/test_prompts.py。
"""

from __future__ import annotations

import re
from typing import Any

from src.app.enums import HintName, Verdict

HINT_NAMES = tuple(h.value for h in HintName)

WORK_TITLE_RX = re.compile(
    r"(\(\s*(19|20)\d{2}\s*(film|movie|tv series|web series|series|album|song|novel|book|game)\s*\)"
    r"|\b(imdb|letterboxd|rottentomatoes|rotten tomatoes|fandom)\b"
    r"|\b(film|movie|web series|tv series|box office|trailer|soundtrack|full movie)\b)",
    re.I,
)


def serp_brief(data: dict[str, Any] | None) -> list[dict[str, str]]:
    """只把前 6 名的域名 + 标题交给 AI；完整前 10 与竞争度由 KV 的 serp:v1: 提供。"""
    items = (data or {}).get("items") or []
    if not items:
        comp = (data or {}).get("competition") or {}
        return [{"domain": str(d), "title": ""} for d in (comp.get("domains") or [])][:6]
    return [{"domain": str(x.get("domain") or ""),
             "title": str(x.get("title") or "")}
            for x in items if isinstance(x, dict)][:6]


def work_signal(items: list[dict[str, str]]) -> str:
    """SERP 标题里的作品信号：前 6 名至少 2 条命中才算硬证据（不依赖 AI 认不认得）。"""
    hits = [x for x in items[:6] if x.get("title") and WORK_TITLE_RX.search(str(x["title"]))]
    if len(hits) < 2:
        return ""
    title = re.sub(r"\s+", " ", hits[0]["title"])[:46]
    return "SERP 标题显示这是具体作品（" + title + "）"


def verdict_of(text: str) -> str:
    """最后一节「结论」：做 / 观望 / 不做；小节号可能变，不写死数字。"""
    s = str(text or "")
    m = re.search(r"\d+\)\s*结论[：:]\s*(不做|观望|做)", s)
    if m:
        return m.group(1)
    if re.search(r"结论[：:][^\n]*不做", s):
        return Verdict.SKIP.value
    return ""


def redline_of(text: str) -> str:
    """AI 漏写「不做」时的兜底：真人姓名 / 影视作品，宁可错杀。"""
    s = str(text or "")
    if re.search(r"(真人姓名|姓名权|肖像权|侵犯.{0,4}权|侵权风险)", s):
        return "真人姓名，涉姓名权/肖像权"
    if (re.search(r"(影视作品|电影|剧集|作品名|专辑|小说|番剧)", s)
            and re.search(r"(版权|侵权|合规|盗版)", s)):
        return "影视/作品名，涉版权与合规风险"
    return ""


def signals_of(text: str) -> list[str] | None:
    """第 8 节信号标签；解析不出返回 None（保持原样），写「无」返回空列表。"""
    m = re.search(r"(?:^|\n)\s*8\)\s*信号标签[：:]\s*([^\n]+)", str(text or ""))
    if not m:
        return None
    line = m.group(1).strip()
    if line.startswith("无"):
        return []
    out = [h for h in HINT_NAMES if h in line][:3]
    return out or None


def decide(text: str, signal: str = "") -> tuple[str, str]:
    """结论 + 理由。红线与 SERP 作品硬证据都能把结论压成「不做」。"""
    verdict = verdict_of(text)
    reason = ""
    risk = redline_of(text)
    if verdict != Verdict.SKIP.value and risk:
        verdict, reason = Verdict.SKIP.value, risk
    if verdict != Verdict.SKIP.value and signal:
        verdict, reason = Verdict.SKIP.value, signal
    return verdict, reason
