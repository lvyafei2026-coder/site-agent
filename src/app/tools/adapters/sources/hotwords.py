# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析：批量取「未跟进」候选 → 补竞争度/趋势 → LLM 出站点方案 → 回写状态。

2026-11-12 从 site-console 的「开始分析」链路整体迁入（radar 的 /api/analyze 与
src/ai.js 同时下线）。提示词、8 小节结构、结论/红线/信号标签判据逐字对齐原实现，
保证 Agent 分析完落在同一批 KV 键上，页面看到的东西和原来点「开始分析」一致：

  /api/serp   → serp:v1:* + competition（竞争度分档、前 10 站点）
  /api/trend  → trend:v1:* + interests（30 天曲线、行内迷你图）
  LLM 正文     → /api/hotwords POST → ai:v3:<词> + aihints
  结论「不做」 → rejected（source=ai，人工标记优先）
  结论「做」   → saved.status=todo（待开发，本轮新增状态）
  结论「观望」 → 只留正文与标签，状态仍为未跟进

「趋势图 / 竞争度」的抓取仍走 radar：那两个接口是采集能力的唯一实现，且写的是
共享 KV，console 的候选页、收藏看板都会自动读到，不需要在 Python 里重写一遍。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from src.app.config import (
    CONSOLE_HQR, CONSOLE_PASS, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, RADAR_URL,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 提示词：与 radar/src/ai.js 的 AI_SYSTEM 逐字一致（那边已删除，这里是唯一真源）
# ---------------------------------------------------------------------------
AI_SYSTEM = """你是给独立开发者做 SEO 工具站的顾问。用户给出一个 Google 搜索热词、它的关联查询（这些是真实的长尾搜索）、命中的常青信号、覆盖地区数和热度值（Google 相对热度，不是搜索量）。

请结合关联查询判断这个词值不值得做站，以及做成什么样的站能同时吃到主词和长尾的搜索流量。严格按下面的小节输出，全中文，总字数 450 以内；不要复述输入，不要写客套话，不要编造具体统计数字。

1) 词义与意图：先说清这个词到底是什么（具体作品名 / 真人姓名 / 品牌或型号 / 通用名词或概念），依据是什么；再一句话说用户搜它想要什么。不确定就直说不确定，这一步不许跳过直接给方案。
2) 关联词解读：逐个分析关联查询（有几个写几个，最多 5 条），每条以「·」开头，格式为「关联词 → 用户在问什么 / 应该做成什么内容」，关联词要原样写出；没有关联查询就写「无关联查询」。
3) 建议站型：一句话说清做成什么页面（计算器 / 对照表 / 倒计时 / 清单 / 长尾合集 …），说明为什么是这种形态，并点出它如何同时接住关联词里的需求。
4) 首页模块：4-6 条，顺序即页面顺序，每条以「·」开头，写清模块名和它解决什么。
5) 差异化：大媒体和现有站点不会做、而我们可以做的部分；如果给了「现有搜索结果」，要针对这几个站没做好的具体点来讲，不要泛泛而谈。
6) 变现：Ads / affiliate / lead gen 选一个为主，一句话理由。
7) 结论：做 / 观望 / 不做，一句话理由。
8) 信号标签：从「工具查询、价格规格、日程日期、资格政策、用法教程、对比评测」里挑出真正适用的（最多 3 个，用顿号分隔），只写标签名不要解释；一个都不适用就只写「无」。这一节要按你上面的分析重判，不要照抄输入里已命中的信号。

判断要点，按顺序判：
⓪ 先辨形，再谈做不做：碰到你不熟悉的词——非英文人名、音译词、印地语/阿拉伯语/日语等外来词、拼写奇怪的两三个词组合、看不出词性的短语——绝不能当成普通名词或产品概念硬解。用「现有搜索结果」的标题核对：出现 Wikipedia 带 (2026 film) / (TV series) / (album) / (song) / (novel) 的条目，或者 IMDb、Letterboxd、Rotten Tomatoes、Fandom、豆瓣条目，或者标题里带 film、movie、series、season、cast、trailer、box office、watch online、release date，就说明它是一部具体作品，按 ② 处理。如果既认不出含义、SERP 标题也给不出线索，第 7 节写「观望」，第 1 节直说「含义不确定」，不要凭空编一个工具站方案。
① 真人姓名优先：只要这个词是真人（明星、运动员、网红、主播、政客、企业家、作者…），包括拼写变体、音译写法、姓名加事件（如 xxx release date / xxx net worth / xxx injury），第 7 节一律写「不做」，理由写明「真人姓名，涉姓名权与肖像权，有侵权风险，且用户要的是资讯不是工具」。这类词即使关联词里有 dates、reviews、statistics 也不要写「观望」或「做」；第 3) 节写「不建站」，第 4) 节写「无」，不要硬凑方案。
② 具体作品与品牌：电影、剧集、综艺、动画、专辑、歌曲、小说、游戏等作品名（含上面认出来的），以及品牌、具体型号、球队——版权与内容合规有风险，且官网、垂媒、豆瓣/IMDb、电商详情页已经占满，新站没有位置，第 7 节写「不做」，第 3) 节写「不建站」。
③ 其余情况才正常给方案；关联词里出现明确工具型长尾（calculator、checker、dates、eligibility、compare 之类）时可以写「观望」并说明该做哪个长尾。"""

HINT_NAMES = ("工具查询", "价格规格", "日程日期", "资格政策", "用法教程", "对比评测")

WORK_TITLE_RX = re.compile(
    r"(\(\s*(19|20)\d{2}\s*(film|movie|tv series|web series|series|album|song|novel|book|game)\s*\)"
    r"|\b(imdb|letterboxd|rottentomatoes|rotten tomatoes|fandom)\b"
    r"|\b(film|movie|web series|tv series|box office|trailer|soundtrack|full movie)\b)",
    re.I,
)

MAX_TOKENS = 900
BATCH_WORKERS = 4           # 20 条逐条分析，并发压住 /api/agent/run 的代理超时
SERP_GAP_SEC = 1.5          # DDG 连查会回 202 挑战页，全局串行 + 最小间隔
_serp_lock = threading.Lock()
_last_serp_at = 0.0


# ---------------------------------------------------------------------------
# 纯判据（无 IO，单测直接喂文本；口径与 console/public/assets/app.js 一致）
# ---------------------------------------------------------------------------
def build_messages(body: dict[str, Any]) -> list[dict[str, str]]:
    """拼 system + user；SERP 前 6 的「域名 — 标题」是 AI 辨形的主要线索。"""
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

    user = "\n".join(x for x in [
        "关键词：" + term,
        "关联查询：" + " / ".join(related) if related else "关联查询：（无）",
        serp_line,
        "命中的常青信号：" + hints if hints else "未命中明显常青信号",
        "覆盖地区数：" + str(geo_count) + " 个",
        "热度（Google 相对值）：" + str(traffic),
    ] if x)
    return [{"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": user}]


def serp_brief(data: dict[str, Any] | None) -> list[dict[str, str]]:
    """只把前 6 名的域名 + 标题交给 AI；完整前 10 与竞争度由 radar 写 KV。"""
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
        return "不做"
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
    if verdict != "不做" and risk:
        verdict, reason = "不做", risk
    if verdict != "不做" and signal:
        verdict, reason = "不做", signal
    return verdict, reason


# ---------------------------------------------------------------------------
# 外部 IO（只允许在这一层发生）
# ---------------------------------------------------------------------------
def _console(method: str, path: str, *, params=None, payload=None) -> dict[str, Any]:
    resp = requests.request(
        method, CONSOLE_HQR.rstrip("/") + path,
        params=params, json=payload,
        auth=("agent", CONSOLE_PASS), timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _radar(path: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(
        RADAR_URL.rstrip("/") + path, params=params,
        auth=(os.environ.get("RADAR_USER", ""), os.environ.get("RADAR_PASS", "")),
        timeout=40,
    )
    resp.raise_for_status()
    return resp.json()


def _llm(messages: list[dict[str, str]]) -> tuple[str, str]:
    """非流式（流式在这条链路上极不稳定）；无 key 返回空，由调用方跳过、绝不编正文。"""
    if not LLM_API_KEY:
        return "", ""
    from openai import OpenAI

    kwargs: dict[str, str] = {"api_key": LLM_API_KEY}
    if LLM_BASE_URL:
        kwargs["base_url"] = LLM_BASE_URL
    client = OpenAI(**kwargs)
    resp = client.chat.completions.create(
        model=LLM_MODEL, messages=messages,
        max_tokens=MAX_TOKENS, temperature=0, timeout=60,
    )
    return (resp.choices[0].message.content or "").strip(), LLM_MODEL


def _throttled_serp(term: str) -> tuple[dict[str, Any] | None, str]:
    """radar /api/serp 自带退避重试；这里再全局串行一次，避免 4 并发同时打 DDG。"""
    global _last_serp_at
    with _serp_lock:
        wait = _last_serp_at + SERP_GAP_SEC - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_serp_at = time.monotonic()
        try:
            data = _radar("/api/serp", {"term": term})
            return (data if data.get("ok") else None), ""
        except Exception as exc:  # noqa: BLE001 - 竞争度失败不拦 AI
            log.warning("SERP 失败 %s: %s", term, exc)
            return None, str(exc)[:120]


def _trend(term: str) -> tuple[bool, str]:
    try:
        data = _radar("/api/trend", {"term": term})
        return bool(data.get("ok")), ""
    except Exception as exc:  # noqa: BLE001 - 趋势失败不影响结论
        log.warning("TREND 失败 %s: %s", term, exc)
        return False, str(exc)[:120]


# ---------------------------------------------------------------------------
# 编排：一次 20 条，逐条走完「补数据 → 分析 → 回写状态」
# ---------------------------------------------------------------------------
def _analyze_one(item: dict[str, Any]) -> dict[str, Any]:
    term = str(item.get("term") or "").strip()
    out: dict[str, Any] = {"term": term, "verdict": "", "status": "", "model": "",
                          "serp": False, "trend": False, "error": ""}
    if not term:
        out["error"] = "空词"
        return out

    serp_data, serp_err = _throttled_serp(term)
    out["serp"] = serp_data is not None
    trend_ok, trend_err = _trend(term)
    out["trend"] = trend_ok

    brief = serp_brief(serp_data)
    signal = work_signal(brief)
    body = dict(item, serp=brief)
    try:
        text, model = _llm(build_messages(body))
    except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
        out["error"] = "LLM 调用失败：" + str(exc)[:160]
        return out
    if not text:
        out["error"] = "LLM 无正文（LLM_API_KEY 未配置或返回为空）"
        return out
    out["model"] = model

    verdict, reason = decide(text, signal)
    out["verdict"] = verdict
    hints = signals_of(text)

    payload: dict[str, Any] = {
        "term": term, "text": text, "verdict": verdict, "reason": reason,
        "cand": {k: item.get(k) for k in
                 ("term", "related", "hints", "geos", "geoCount", "traffic", "count", "score")},
    }
    if hints is not None:
        payload["hints"] = hints
    try:
        saved = _console("POST", "/api/hotwords", payload=payload)
    except Exception as exc:  # noqa: BLE001 - 回写失败保留正文与诊断
        out["error"] = "状态回写失败：" + str(exc)[:160]
        return out
    out["status"] = "todo" if saved.get("status") == "todo" else (
        "rejected" if saved.get("rejected") else "")
    if serp_err or trend_err:
        out["error"] = ("SERP 未取到：" + serp_err if serp_err else "") + (
            "；趋势未取到：" + trend_err if trend_err else "")
    return out


def run_batch(limit: int = 20) -> dict[str, Any]:
    """GET 待分析词 → 并发逐条分析 → 汇总。console 不可达时整批失败，不写任何状态。"""
    limit = max(1, min(int(limit or 20), 50))
    try:
        pending = _console("GET", "/api/hotwords", params={"limit": limit})
    except Exception as exc:  # noqa: BLE001
        log.warning("候选池读取失败：%s", exc)
        return {"ok": False, "error": "候选池读取失败：" + str(exc)[:200],
                "items": [], "fetched": 0, "analyzed": 0, "failed": 0, "counts": {}}
    items = pending.get("items") or []
    if not items:
        return {"ok": True, "items": [], "fetched": 0, "analyzed": 0,
                "failed": 0, "counts": {}, "note": "没有未跟进且未分析过的候选词"}

    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        results = list(pool.map(_analyze_one, items))

    counts: dict[str, int] = {}
    for r in results:
        key = r["verdict"] or "未定"
        counts[key] = counts.get(key, 0) + 1
    ok = sum(1 for r in results if not r["error"])
    return {
        "ok": ok > 0,
        "fetched": len(items),
        "analyzed": ok,
        "failed": len(results) - ok,
        "counts": counts,
        "items": results,
    }
