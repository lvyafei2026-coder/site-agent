# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析的纯判据层：提示词拼装、LLM 输出解析、结论/红线/信号标签判定。

这里**没有任何外部 IO**（不碰 KV / LLM / 抓取），全部函数都是「文本进、文本出」，
不依赖外部状态。口径与 console/public/assets/app.js 一致。

拆分自 hotwords.py（2026-11-17）：该文件当时 1200+ 行，判据、LLM 调用、KV 写入、
编排逻辑混在一起；本模块承载其中「判读」这一层。编排已于 2026-11-20 分层归位成
graph（热词图装配 + 运行入口）、nodes（循环节点）、services（单词编排）三层，
IO 落在同包的 fetch / writeback，经 tools 层的工具调用；
LLM 传输本身是与业务无关的横切件，在 src/app/utils/llm_io。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Optional

from src.app.config import settings
from src.app.enums import HintName, PromptTask, Verdict
# ★adapters 不能反向依赖 services（见 test_architecture.py），所以走 prompts 包的纯加载函数，
#   而不是 services/prompt_service.py 那个带日志的节点入口。
from src.app.prompts import load_prompt

# ---------------------------------------------------------------------------
# 提示词：正文全部在 D1 的 prompts 表（hotwords / hotwords_user 两 task）。
#   这里只做「加载 + 进程内缓存」：一批 20 个词不能每词都拉一次 D1。
#   ★换措辞在 /rules/prompts 页面新增 vN 并切默认，最多 60 秒生效、不用重启。
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8)
def _prompt_pair(version: Optional[str] = None) -> tuple[str, str]:
    """返回 (system, user 模板)。version 由 /run 的 prompt_versions.hotwords 透传。"""
    v = version or (settings.default_prompt_versions or {}).get(PromptTask.HOTWORDS.value, "v1")
    return load_prompt(PromptTask.HOTWORDS.value, v), load_prompt(PromptTask.HOTWORDS_USER.value, v)

# 判例反馈：从人工「不做」标记里挑少量真实词例喂回 AI，让它对同类词倾向判不做。
# ★v2 提示词已固化高频规则（非工具意图/时效资讯/热度下降/真人/作品），这里的判例
#   只取「人工判定 + 有泛化价值理由」的词，避开已固化的占位理由，防止提示词与判例重复。
EXAMPLE_LIMIT = 8
# 这些理由已写进 v2 提示词的硬规则，再当判例注入只是重复占位、浪费 token。
_EXAMPLE_SKIP_RX = re.compile(
    r"搜索意图不是找工具|热度下降|趋势下降|未填原因|旧版本本地标记"
)


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


def examples_line(examples: list[dict[str, str]]) -> str:
    """把判例拼成 user 模板的 ``{examples_line}`` 占位内容；无判例返回空串。"""
    if not examples:
        return ""
    head = "以下是你过去判定「不做」的真实案例，遇到同类词请优先判不做："
    rows = [f"· {e['term']} — {e['reason']}" for e in examples]
    return head + "\n" + "\n".join(rows)


HINT_NAMES = tuple(h.value for h in HintName)

WORK_TITLE_RX = re.compile(
    r"(\(\s*(19|20)\d{2}\s*(film|movie|tv series|web series|series|album|song|novel|book|game)\s*\)"
    r"|\b(imdb|letterboxd|rottentomatoes|rotten tomatoes|fandom)\b"
    r"|\b(film|movie|web series|tv series|box office|trailer|soundtrack|full movie)\b)",
    re.I,
)


# 逐词分析要输出 8 小节、450 字以内的中文正文（约 600-900 token）；这是本线的输出
# 口径，所以留在这里而不是传输层（utils/llm_io.complete 只收 max_tokens 参数）。
MAX_TOKENS = 2000


def build_messages(body: dict[str, Any], *, prompt_version: Optional[str] = None,
                   examples: Optional[list[dict[str, str]]] = None) -> list[dict[str, str]]:
    """拼 system + user；**措辞全部来自 D1 的 hotwords 提示词**，这里只填数据。

    SERP 前 6 的「域名 — 标题」是 AI 辨形的主要线索。模板由 _prompt_pair() 按
    prompt_version 一次性加载并缓存，20 个词只读一次。

    ``examples`` 是人工判例反馈（见 :func:`pick_examples`）：仅当 user 模板含
    ``{examples_line}`` 占位符（v2+）时注入；v1 模板无此占位符则输出逐字不变。
    """
    system, user_template = _prompt_pair(prompt_version)

    term = str(body.get("term") or "")[:120]
    related = [str(x) for x in (body.get("related") or [])][:6]
    hints = "、".join(str(x) for x in (body.get("hints") or []))
    geo_count = body.get("geoCount") or 0
    traffic = body.get("traffic") or 0

    serp = [x for x in (body.get("serp") or []) if x][:6]
    lines = []
    for item in serp:
        if isinstance(item, str):
            lines.append("· " + item)
            continue
        domain = str(item.get("domain") or "")
        title = re.sub(r"\s+", " ", str(item.get("title") or ""))[:90]
        lines.append("· " + domain + (" — " + title if title else ""))
    serp_line = "现有搜索结果前几名（域名 — 页面标题）：\n" + "\n".join(lines) if lines else ""
    related_line = "关联查询：" + " / ".join(related) if related else "关联查询：（无）"
    hints_line = "命中的常青信号：" + hints if hints else "未命中明显常青信号"

    # 模板里没有 SERP 时是一整行空占位，过滤空行后与原拼装结果一致。
    fields = dict(term=term, related_line=related_line, serp_line=serp_line,
                  hints_line=hints_line, geo_count=geo_count, traffic=traffic)
    # ★只有模板真的含 {examples_line} 才传该字段：v1 模板没有它，传了会 KeyError。
    #   build_messages 的输出格式与 D1 模板占位符对应，见 test_prompts.py。
    if "{examples_line}" in user_template:
        fields["examples_line"] = examples_line(examples or [])
    user = "\n".join(x for x in user_template.format(**fields).split("\n") if x)
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


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
