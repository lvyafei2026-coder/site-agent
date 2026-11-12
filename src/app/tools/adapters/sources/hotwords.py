# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析：批量取「未跟进」候选 → 补竞争度/趋势 → LLM 出站点方案 → 直写共享 KV。

2026-11-12 去接口化改造：本模块**不再调用** site-console 的 /api/hotwords 与
google-trend-radar 的 /api/serp、/api/trend、/api/candidates。它直接读写三方共用的共享 KV
（Cloudflare KV REST API，见 kv.py），数据链路只有「存储介质依赖」，没有「服务接口依赖」：

  候选池    → 读 KV `candidates`
  未跟进判定 → 读 KV `saved` / `rejected` / `tags` + 列举 `ai:v3:` 前缀
  竞争度/趋势 → 读 KV `serp:v1:<词>` / `trend:v1:<词>`（缺失时由本模块用 scrape 模块
              现抓并写回 KV，同时更新 `competition` / `interests` 摘要表）
  LLM 正文  → 直写 KV `ai:v3:<词>`
  信号标签  → 直写 KV `aihints`
  结论「不做」→ 直写 KV `rejected`（source=ai，人工标记优先）
  结论「做」  → 直写 KV `saved`（status=todo，待开发）
  结论「观望」→ 只留正文与标签，状态仍为未跟进

提示词、8 小节结构、结论/红线/信号标签判据逐字对齐 radar 原 src/ai.js（那边已删除，此处唯一真源）。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from .kv import (
    CloudflareKV, get_kv, term_key,
    AI_CACHE_PREFIX, SERP_PREFIX, TREND_PREFIX,
    SAVED_KEY, REJECTED_KEY, TAGS_KEY, COMPETITION_KEY, INTERESTS_KEY, HINTS_KEY,
)
from . import scrape_serp, scrape_trend
from .curation import upsert_saved, upsert_rejected, set_hint_override

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

WORK_TITLE_RX = __import__("re").compile(
    r"(\(\s*(19|20)\d{2}\s*(film|movie|tv series|web series|series|album|song|novel|book|game)\s*\)"
    r"|\b(imdb|letterboxd|rottentomatoes|rotten tomatoes|fandom)\b"
    r"|\b(film|movie|web series|tv series|box office|trailer|soundtrack|full movie)\b)",
    __import__("re").I,
)

MAX_TOKENS = 900
BATCH_WORKERS = 4           # 20 条逐条分析，并发压住外部 LLM 超时
SERP_GAP_SEC = 1.5          # DDG 连查会回 202 挑战页，全局串行 + 最小间隔
SERP_TTL = 604800           # serp:v1: 缓存 7 天
TREND_TTL = 259200          # trend:v1: 缓存 3 天

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
        title = __import__("re").sub(r"\s+", " ", str(item.get("title") or ""))[:90]
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
    title = __import__("re").sub(r"\s+", " ", hits[0]["title"])[:46]
    return "SERP 标题显示这是具体作品（" + title + "）"


def verdict_of(text: str) -> str:
    """最后一节「结论」：做 / 观望 / 不做；小节号可能变，不写死数字。"""
    s = str(text or "")
    m = __import__("re").search(r"\d+\)\s*结论[：:]\s*(不做|观望|做)", s)
    if m:
        return m.group(1)
    if __import__("re").search(r"结论[：:][^\n]*不做", s):
        return "不做"
    return ""


def redline_of(text: str) -> str:
    """AI 漏写「不做」时的兜底：真人姓名 / 影视作品，宁可错杀。"""
    s = str(text or "")
    if __import__("re").search(r"(真人姓名|姓名权|肖像权|侵犯.{0,4}权|侵权风险)", s):
        return "真人姓名，涉姓名权/肖像权"
    if (__import__("re").search(r"(影视作品|电影|剧集|作品名|专辑|小说|番剧)", s)
            and __import__("re").search(r"(版权|侵权|合规|盗版)", s)):
        return "影视/作品名，涉版权与合规风险"
    return ""


def signals_of(text: str) -> list[str] | None:
    """第 8 节信号标签；解析不出返回 None（保持原样），写「无」返回空列表。"""
    m = __import__("re").search(r"(?:^|\n)\s*8\)\s*信号标签[：:]\s*([^\n]+)", str(text or ""))
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
# 外部 IO（只允许在这一层发生，且全部走 KV / 外部 LLM / 外部抓取，不调 console/radar 接口）
# ---------------------------------------------------------------------------
def _llm(messages: list[dict[str, str]]) -> tuple[str, str]:
    """非流式（流式在这条链路上极不稳定）；无 key 返回空，由调用方跳过、绝不编正文。"""
    from src.app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
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


def _throttled_serp(term: str, kv: CloudflareKV) -> tuple[dict[str, Any] | None, str]:
    """KV 已有 serp:v1: 直接读；缺失则现抓并写回。全局串行避免 4 并发同时打 DDG。"""
    key = term_key(term)
    cached = kv.get_json(SERP_PREFIX + key)
    if cached:
        return cached, ""
    global _last_serp_at
    with _serp_lock:
        wait = _last_serp_at + SERP_GAP_SEC - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_serp_at = time.monotonic()
    try:
        s = scrape_serp.fetch_serp(term)
    except Exception as exc:  # noqa: BLE001 - 竞争度失败不拦 AI
        log.warning("SERP 失败 %s: %s", term, exc)
        return None, str(exc)[:120]
    comp = scrape_serp.score_competition(s["items"], term)
    payload = {"engine": s["engine"], "items": s["items"], "competition": comp, "at": int(time.time() * 1000)}
    kv.put_json(SERP_PREFIX + key, payload, ttl=SERP_TTL)
    comp_map = kv.get_json(COMPETITION_KEY, {}) or {}
    comp_map[key] = {"score": comp["score"], "level": comp["level"], "at": payload["at"]}
    kv.put_json(COMPETITION_KEY, comp_map)
    return payload, ""


def _trend(term: str, kv: CloudflareKV) -> tuple[bool, str]:
    """KV 已有 trend:v1: 直接读；缺失则现抓并写回 + 更新 interests 摘要表。"""
    key = term_key(term)
    cached = kv.get_json(TREND_PREFIX + key)
    if cached:
        return True, ""
    try:
        t = scrape_trend.fetch_interest(term)
    except Exception as exc:  # noqa: BLE001 - 趋势失败不影响结论
        log.warning("TREND 失败 %s: %s", term, exc)
        return False, str(exc)[:120]
    payload = {"points": t["points"], "summary": t["summary"], "at": int(time.time() * 1000)}
    kv.put_json(TREND_PREFIX + key, payload, ttl=TREND_TTL)
    int_map = kv.get_json(INTERESTS_KEY, {}) or {}
    int_map[key] = {
        "term": term, "summary": t["summary"], "at": payload["at"],
        "points": [p["v"] for p in t["points"]],
    }
    kv.put_json(INTERESTS_KEY, int_map)
    return True, ""


# ---------------------------------------------------------------------------
# 编排：一次 20 条，逐条走完「补数据 → 分析 → 回写状态」；全部读写共享 KV
# ---------------------------------------------------------------------------
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


def _select_pending(kv: CloudflareKV, limit: int) -> list[dict[str, Any]]:
    """读 KV 候选池，剔除已跟进/打标签/已分析的词，返回前 limit 条未跟进候选。

    ★形状兜底：KV 里这几张表是 console/radar 写的，字段形状一旦漂移（比如 tags 变成数组、
    candidates 变成对象），按原样 .items() / 迭代 会整批失败。这里统一按「期望类型不符就
    当空」处理并 warn，让漂移可见但不阻断分析。
    """
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

    items: list[dict[str, Any]] = []
    for c in candidates:
        if not isinstance(c, dict) or not c.get("term"):
            continue
        key = term_key(c.get("key") or c.get("term"))
        if key in curated or key in analyzed:
            continue
        items.append({
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
        })
        if len(items) >= limit:
            break
    return items


def _analyze_one(item: dict[str, Any], kv: CloudflareKV) -> dict[str, Any]:
    term = str(item.get("term") or "").strip()
    out: dict[str, Any] = {"term": term, "verdict": "", "status": "", "model": "",
                           "serp": False, "trend": False, "error": ""}
    if not term:
        out["error"] = "空词"
        return out

    serp_data, serp_err = _throttled_serp(term, kv)
    out["serp"] = serp_data is not None
    trend_ok, trend_err = _trend(term, kv)
    out["trend"] = trend_ok

    brief = serp_brief(serp_data)
    signal = work_signal(brief)
    try:
        text, model = _llm(build_messages(dict(item, serp=brief)))
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

    # 直写 KV（不经由 console 的 /api/hotwords）
    key = term_key(term)
    kv.put_text(AI_CACHE_PREFIX + key, text)
    if hints is not None:
        hints_map = _kv_dict(kv, HINTS_KEY)
        kv.put_json(HINTS_KEY, set_hint_override(hints_map, term, hints))

    if verdict == "不做":
        before = _kv_list(kv, REJECTED_KEY)
        kv.put_json(REJECTED_KEY, upsert_rejected(before, {"term": term, "reason": reason or "AI 判定不做", "source": "ai"}))
        out["status"] = "rejected"
    elif verdict == "做":
        before = _kv_list(kv, SAVED_KEY)
        cand = {k: item.get(k) for k in ("term", "related", "hints", "geos", "geoCount", "traffic", "count", "score")}
        kv.put_json(SAVED_KEY, upsert_saved(before, {**cand, "term": term, "status": "todo", "verdict": "做"}))
        out["status"] = "todo"
    # 「观望」与解析不出结论：只留正文和标签，状态保持未跟进

    if serp_err or trend_err:
        out["error"] = ("SERP 未取到：" + serp_err if serp_err else "") + (
            "；趋势未取到：" + trend_err if trend_err else "")
    return out


def run_batch(limit: int = 20, kv: Optional[CloudflareKV] = None) -> dict[str, Any]:
    """读 KV 候选池 → 并发逐条分析 → 汇总。KV 不可达时整批失败，不写任何状态。"""
    kv = kv or get_kv()
    limit = max(1, min(int(limit or 20), 50))
    try:
        items = _select_pending(kv, limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("候选池读取失败：%s", exc)
        return {"ok": False, "error": "候选池读取失败：" + str(exc)[:200],
                "items": [], "fetched": 0, "analyzed": 0, "failed": 0, "counts": {}}

    if not items:
        return {"ok": True, "items": [], "fetched": 0, "analyzed": 0,
                "failed": 0, "counts": {}, "note": "没有未跟进且未分析过的候选词"}

    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        results = list(pool.map(lambda it: _analyze_one(it, kv), items))

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
