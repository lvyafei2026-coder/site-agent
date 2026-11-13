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

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from src.app.config import settings
from src.app.observability.langsmith import run_traced, trace_tool, tracing_enabled

from .kv import (
    CloudflareKV, get_kv, term_key, atomic_update, table_lock,
    AI_CACHE_PREFIX, SERP_PREFIX, TREND_PREFIX,
    SAVED_KEY, REJECTED_KEY, TAGS_KEY, COMPETITION_KEY, INTERESTS_KEY, HINTS_KEY,
)
from . import scrape_serp, scrape_trend
from .errors import SourceUnavailable
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

# 逐词分析要输出 8 小节、450 字以内的中文正文（约 600-900 token）。
# ★DeepSeek 思考模式默认开启（effort=high），reasoning_content 单独返回且计入
#   max_tokens：实测 2000 全被推理吃光 → finish_reason=length、content 为空。
#   所以逐词调用会关掉思考模式（见 _thinking_kwargs），让这 2000 全给正文；
#   若手动设 LLM_THINKING=on 保留思考，需自行把这里调大。
MAX_TOKENS = 2000
BATCH_WORKERS = 4           # 20 条逐条分析，并发压住外部 LLM 超时
BATCH_OK_MIN_RATE = 0.5     # 整批成功判定：成功率低于此值即视为失败（不再「1 条成功就算成功」）
SERP_GAP_SEC = 1.5          # 全局串行 + 最小间隔；202 的根因是请求头，见 scrape_serp.py
SERP_TTL = 604800           # serp:v1: 缓存 7 天
TREND_TTL = 259200          # trend:v1: 缓存 3 天

# ★2026-11-13：LangSmith 一条根 trace 的层级 =
#   graph 根 → 批次 span → 每词 span → [tool:serp / tool:trend / ChatOpenAI / tool:kv.write]。
#   ThreadPoolExecutor 的 worker 线程不继承主线程 contextvars，父 run 必须显式传（见
#   observability/langsmith.py:run_traced）；词 span 拿到自己的 run 后，再把它作为
#   所有 tool span 的 parent，SERP/Trends/KV 这些「原本只存在于日志里」的调用才会
#   出现在 Traces 页面上。graph 结构本身一个字都不改。
BATCH_TRACE_NAME = "site-agent:hotword-batch"
WORD_TRACE_PREFIX = "site-agent:hotword:"
TOOL_TRACE_PREFIX = "tool:"

# ★2026-11-13（用户定）：批次不再因某一个词/某一个源失败而中止——「完成一个结束一个、互不影响」。
#   ・SERP 源整体不可用 → 只让该词跳过 SERP（notice 里标明缺辨形线索），本批后续词也跳过，
#     避免每个词都撞一遍 4 发超时；LLM 照常出结论。
#   ・LLM 连败 → 只记该词 error，不再 trip 停掉整批。
#   ・任何 put_* 失败 → 该词记 error、不算 analyzed；正文已写但状态表丢失的词由
#     backfill_states() 从正文重新补写状态（否则 _select_pending 会永远剔除它）。
#   ABORT_ON_SERP_DOWN 因此删除：不再保留「整批中止」这条分支。

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
class _WriteLog:
    """单词级写入账本：这个词的每一次 put_* 都记结果，失败则汇总成 error。

    以前 `kv.put_json(...)` 的返回值被丢掉，Cloudflare 返回 401/5xx 时进程内
    仍以为写成功了，于是「正文有了、状态表没有」，而 _select_pending 又会因为
    正文存在把这个词永久剔除。现在每次写都检查，失败清单会进 items[].error。
    """

    def __init__(self, term: str = "") -> None:
        self.term = term
        self.failed: list[str] = []
        # 每次写的明细（供 LangSmith 的 tool:kv.write / tool:serp / tool:trend 输出）。
        # 关闭追踪时只记 key/kind/phase/ok 四个小字段，不算体积，零额外开销。
        self.records: list[dict[str, Any]] = []
        self.phase = "analysis"   # serp / trend / analysis
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


def _tool_step(name: str, call, *, inputs: dict[str, Any], outputs,
               parent, term: str):
    """逐词内部的一步 IO = 一条 `tool:*` span，显式挂在词 span 下。

    ★不吞异常：SERP/Trends 源不可用时 SDK 把 traceback 写进 span.error 并标红，
      业务侧照常 catch 后降级。这样 Smith 上既看得到「调用过」，也看得到「为什么跳过」。
    """
    return trace_tool(
        call,
        name=TOOL_TRACE_PREFIX + name,
        inputs=inputs,
        parent=parent,                # ★worker 线程：必须显式传词 span 的 run
        tags=["site-agent", "hotword-tool"],
        metadata={"term": term, "component": "analyze_hotwords"},
        outputs=outputs,
    )

    def error(self) -> str:
        return "KV 写入失败：" + "、".join(self.failed) if self.failed else ""


class LLMEmptyContent(Exception):
    """LLM 调用已完成但没有可用正文。message 是准确的、可操作的失败说明。

    与「LLM 调用失败」（连接/鉴权异常）分开：这类是本词没产出内容，不代表整批
    模型不可用，调用方不应据此中止整批。
    """

    def __init__(self, message: str, diag: dict[str, Any]) -> None:
        super().__init__(message)
        self.diag = diag


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """响应可能是标准对象、wrap_openai 的 dict、或 None，统一取值。"""
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _thinking_kwargs() -> dict[str, Any]:
    """DeepSeek 思考模式默认开启，逐词分析这类固定格式的文案任务需要关掉。

    auto（默认）：仅当 LLM_BASE_URL 指向 DeepSeek 时注入 extra_body，换用别的
      OpenAI 兼容服务不会误传这个 DeepSeek 专属参数；
    off：强制注入；on：保留思考（此时要自行调大 MAX_TOKENS）。
    """
    from src.app.config import LLM_BASE_URL, LLM_THINKING

    mode = (LLM_THINKING or "auto").strip().lower()
    if mode == "on":
        return {}
    if mode == "off" or (mode == "auto" and "deepseek" in (LLM_BASE_URL or "").lower()):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def _empty_content_diag(resp: Any) -> dict[str, Any]:
    """把「正文为空」的现场读全，并给出与真实原因一致的错误说明。

    实测线上场景：finish_reason=length、content=""、reasoning_tokens=2000、
    completion_tokens=2000 —— 是输出预算被思考推理吃光，绝不是 API Key 没配。
    """
    from src.app.config import LLM_MODEL

    choices = _field(resp, "choices") or []
    first = choices[0] if choices else None
    finish = _field(first, "finish_reason")
    usage = _field(resp, "usage") or {}
    details = _field(usage, "completion_tokens_details") or {}
    reasoning = _field(details, "reasoning_tokens")
    if reasoning is None:                       # 兼容扁平 usage / LangSmith 形态
        reasoning = _field(usage, "reasoning_tokens")
    if reasoning is None:
        out_details = _field(_field(resp, "usage_metadata") or {},
                             "output_token_details") or {}
        reasoning = _field(out_details, "reasoning")
    completion = _field(usage, "completion_tokens")
    model = _field(resp, "model") or LLM_MODEL

    if finish == "length" and reasoning:
        reason = "输出预算耗尽（思考推理吃光 max_tokens，正文未生成）"
        hint = "关闭思考模式（LLM_THINKING=off/auto）或调大 MAX_TOKENS"
    elif finish == "length":
        # 已经没在思考却还是 length，说明是正文自己超了预算，不能赖到推理头上
        reason = "输出预算耗尽（正文超出 MAX_TOKENS，本次未产生推理 token）"
        hint = "调大 MAX_TOKENS（当前 2000）"
    elif not choices:
        reason = "响应中没有 choices"
        hint = "检查 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 与响应形态"
    elif _field(first, "message") is None:
        reason = "响应形态异常（choice 缺 message）"
        hint = "检查 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 与响应形态"
    else:
        reason = "模型返回空 content"
        hint = "检查 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 与响应形态"

    diag = {
        "reason": reason, "hint": hint, "model": model,
        "finish_reason": finish, "reasoning_tokens": reasoning,
        "completion_tokens": completion, "max_tokens": MAX_TOKENS,
        "resp_type": type(resp).__name__, "choices": len(choices),
    }
    diag["message"] = (
        f"LLM 返回空正文：{reason}；finish_reason={finish}, "
        f"max_tokens={MAX_TOKENS}, reasoning_tokens={reasoning}, "
        f"completion_tokens={completion}, model={model}, "
        f"resp_type={type(resp).__name__}, choices={len(choices)}；建议：{hint}"
    )
    return diag


def _llm(messages: list[dict[str, str]]) -> tuple[str, str]:
    """逐词分析的非流式 LLM 调用；正文为空抛 LLMEmptyContent（带准确诊断）。

    ★走统一的 get_llm()：与 extract_fields 共用同一客户端（含 LangSmith trace 与
      LLM_DEBUG 的 HTTP 钩子），逐词调用不再是 trace/日志的盲区。响应经
      content_from_response 归一——wrap_openai 会把 ChatCompletion 转成 dict，
      裸写 resp.choices[0].message.content 在这条路径上会直接抛 AttributeError。
    """
    from src.app.config import LLM_API_KEY, LLM_MODEL
    from src.app.llm.provider import content_from_response, get_llm

    if not LLM_API_KEY:
        diag = {"reason": "LLM_API_KEY 未配置，未发起调用", "no_key": True,
                "hint": "在 Render 配好 LLM_API_KEY", "finish_reason": None,
                "reasoning_tokens": None, "max_tokens": MAX_TOKENS}
        diag["message"] = "LLM 未配置 API Key（LLM_API_KEY 为空），本次未发起调用；建议在 Render 配好该变量"
        raise LLMEmptyContent(diag["message"], diag)

    resp = get_llm().chat.completions.create(
        model=LLM_MODEL, messages=messages,
        max_tokens=MAX_TOKENS, temperature=0, timeout=60,
        **_thinking_kwargs(),
    )
    text = (content_from_response(resp) or "").strip()
    if not text:
        diag = _empty_content_diag(resp)
        log.warning("hotwords_llm_empty_content %s", diag["message"])
        raise LLMEmptyContent(diag["message"], diag)
    return text, LLM_MODEL


def _throttled_serp(term: str, kv: CloudflareKV,
                    writes: Optional[_WriteLog] = None,
                    diag: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any] | None, str]:
    """KV 已有 serp:v1: 直接读；缺失则现抓并写回。全局串行避免并发同时打 DDG。

    ★源级不可用（连不上 / 被挑战页占满）原样抛 SourceUnavailable，由 _analyze_one
      **只让本词跳过 SERP**，不再中止整批；其它异常降级为「无 SERP」，不拦 AI。
    ★diag 是给 LangSmith span 的现场记录（是否命中缓存、4 发尝试明细、抓取异常），
      不改变返回值形状，老调用方不受影响。
    """
    diag = {} if diag is None else diag
    diag["fromCache"] = False
    key = term_key(term)
    writes = writes or _WriteLog(term)
    cached = kv.get_json(SERP_PREFIX + key)
    if cached:
        diag["fromCache"] = True
        return cached, ""
    global _last_serp_at
    with _serp_lock:
        wait = _last_serp_at + SERP_GAP_SEC - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_serp_at = time.monotonic()
    try:
        s = scrape_serp.fetch_serp(term)
    except SourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 单词级问题（解析等）不拦 AI
        log.warning("SERP 失败 %s: %s", term, exc)
        diag["fetchError"] = str(exc)[:300]
        return None, str(exc)[:120]
    diag["engine"] = s.get("engine")
    diag["attempts"] = s.get("attempts") or []
    comp = scrape_serp.score_competition(s["items"], term)
    payload = {"engine": s["engine"], "items": s["items"], "competition": comp, "at": int(time.time() * 1000)}
    # 单词缓存键：各词独立、无 read-modify-write，但仍要检查写返回值
    writes.json(kv, SERP_PREFIX + key, payload, ttl=SERP_TTL)
    # competition 是整表：读-改-写必须走原子更新，否则 4 个 worker 会互相覆盖
    entry = {"score": comp["score"], "level": comp["level"], "at": payload["at"]}
    writes.table(kv, COMPETITION_KEY, lambda m: {**m, key: entry}, reader=_kv_dict)
    return payload, ""


def _trend(term: str, kv: CloudflareKV,
           writes: Optional[_WriteLog] = None,
           diag: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
    """KV 已有 trend:v1: 直接读；缺失则现抓并写回 + 更新 interests 摘要表。

    ★源级不可用会抛 SourceUnavailable，由 _analyze_one 把「本批禁用趋势」置位——趋势只是
      辅助证据，不该因为它连不上就让整批分析白跑，但也不该每个词都去撞一遍超时。
    ★diag 同理记录命中缓存 / 抓取点数 / 尝试明细，供 tool:trend span 输出。
    """
    diag = {} if diag is None else diag
    diag["fromCache"] = False
    key = term_key(term)
    writes = writes or _WriteLog(term)
    cached = kv.get_json(TREND_PREFIX + key)
    if cached:
        diag["fromCache"] = True
        diag["pointCount"] = len(cached.get("points") or [])
        return True, ""
    try:
        t = scrape_trend.fetch_interest(term)
    except SourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 单词解析问题不影响结论
        log.warning("TREND 失败 %s: %s", term, exc)
        diag["fetchError"] = str(exc)[:300]
        return False, str(exc)[:120]
    diag["attempts"] = t.get("attempts") or []
    diag["pointCount"] = len(t.get("points") or [])
    diag["summary"] = t.get("summary") or {}
    payload = {"points": t["points"], "summary": t["summary"], "at": int(time.time() * 1000)}
    writes.json(kv, TREND_PREFIX + key, payload, ttl=TREND_TTL)
    entry = {
        "term": term, "summary": t["summary"], "at": payload["at"],
        "points": [p["v"] for p in t["points"]],
    }
    writes.table(kv, INTERESTS_KEY, lambda m: {**m, key: entry}, reader=_kv_dict)
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


class _BatchAbort:
    """批次级**降级状态**（名字沿用历史；2026-11-13 起不再有「中止整批」能力）。

    单词级失败隔离后，这里只保留三件事，全都是「省时间 / 供汇总」，不再拦任何词：

      ・SERP 源不可用   → serp_off：本批后续词跳过 SERP，省掉每词 4 发超时；
                           当前词与后续词都照常走 LLM、照常回写状态（notice 标缺线索）。
      ・Trends 源不可用 → trends_off：同上，趋势只是辅助证据。
      ・LLM 失败        → 只累加次数供汇总，**不 trip、不中断**；该词的 error 由调用方记。

    为什么仍保留批次级开关：源级故障是「出口 IP 不通」这类全局问题，让 20 个词各撞
    一遍 6 秒超时毫无意义；跳过的是「取数」这一步，不是整词分析。
    """

    def __init__(self) -> None:
        self.trends_off = False
        self.serp_off = False
        self.llm_failures = 0
        self._lock = threading.Lock()

    def disable_trends(self) -> bool:
        """首次调用返回 True（用于只打一条日志）；之后恒 False。"""
        with self._lock:
            first = not self.trends_off
            self.trends_off = True
            return first

    def disable_serp(self) -> bool:
        """本批后续词不再尝试 SERP；返回是否首次置位。"""
        with self._lock:
            first = not self.serp_off
            self.serp_off = True
            return first

    def note_llm_failure(self) -> int:
        """记一次 LLM 失败，返回累计次数。只用于日志/汇总，不再据此中止整批。"""
        with self._lock:
            self.llm_failures += 1
            return self.llm_failures


def _finalize(out: dict[str, Any], writes: _WriteLog) -> dict[str, Any]:
    """把本词所有写入失败汇总进 error —— 有任何一个写失败就不算 analyzed。"""
    if writes.failed:
        out["writeErrors"] = list(writes.failed)
        out["error"] = (out["error"] + "；" if out["error"] else "") + writes.error()
    return out


def _analyze_one(item: dict[str, Any], kv: CloudflareKV,
                 state: Optional["_BatchAbort"] = None,
                 parent_run=None) -> dict[str, Any]:
    """一个词走完「补数据 → LLM → 回写状态」，失败只落在这个词自己身上。

    ★不抛异常给上层：SERP/Trends/LLM/KV 任一环出问题都写进 items[].error，
      其它词继续跑（完成一个结束一个、互不影响）。
    ★parent_run 是本词 span 的 RunTree：SERP / Trends / KV 三处 IO 各自建一条
      `tool:*` 子 span 并显式挂在它下面（worker 线程没有主线程 contextvars）。
      追踪关闭时它是 None，`trace_tool` 直接调函数、零开销。
    """
    term = str(item.get("term") or "").strip()
    out: dict[str, Any] = {"term": term, "verdict": "", "status": "", "model": "",
                           "serp": False, "trend": False, "error": ""}
    if not term:
        out["error"] = "空词"
        return out

    writes = _WriteLog(term)
    writes.capture = tracing_enabled()
    key = term_key(term)

    # ---- SERP：读缓存 / 现抓 / 写回，整段是一条 tool:serp span ----
    serp_err = ""
    serp_diag: dict[str, Any] = {}
    writes.phase = "serp"
    serp_mark = len(writes.records)

    def serp_outputs(res):
        data, err = res
        return {
            "engine": (data or {}).get("engine") or serp_diag.get("engine") or "",
            "itemCount": len((data or {}).get("items") or []),
            "items": (data or {}).get("items") or [],
            "competition": (data or {}).get("competition") or {},
            "fromCache": bool(serp_diag.get("fromCache")),
            "attempts": serp_diag.get("attempts") or [],
            "fetchError": serp_diag.get("fetchError") or "",
            "error": err,
            "kvWrites": writes.records[serp_mark:],
        }

    if state is not None and state.serp_off:
        serp_data, serp_err = None, "本批已跳过 SERP（源不可用）"
        # 「跳过」也要留痕：否则 Smith 上这一词像是压根没打算取 SERP
        _tool_step(
            "serp", lambda: (None, serp_err),
            inputs={"term": term, "key": key, "skipped": True,
                    "reason": serp_err},
            outputs=lambda res: {"skipped": True, "reason": res[1],
                                 "kvWrites": []},
            parent=parent_run, term=term,
        )
    else:
        try:
            serp_data, serp_err = _tool_step(
                "serp", lambda: _throttled_serp(term, kv, writes, serp_diag),
                inputs={"term": term, "key": key, "cacheKey": SERP_PREFIX + key},
                outputs=serp_outputs, parent=parent_run, term=term,
            )
        except SourceUnavailable as exc:
            # span 已被 SDK 标红（含完整 traceback）；这里只做业务降级。
            log.warning("SERP 源不可用，本词跳过 SERP 继续分析：%s", exc.brief())
            serp_data, serp_err = None, exc.brief()
            if state is not None and state.disable_serp():
                log.warning("本批后续词跳过 SERP（结论缺少辨形线索）")
    out["serp"] = serp_data is not None

    # ---- Trends：读缓存 / 现抓 / 写回，整段是一条 tool:trend span ----
    trend_ok, trend_err = False, ""
    trend_diag: dict[str, Any] = {}
    writes.phase = "trend"
    trend_mark = len(writes.records)

    def trend_outputs(res):
        ok, err = res
        return {
            "ok": ok, "fromCache": bool(trend_diag.get("fromCache")),
            "pointCount": trend_diag.get("pointCount") or 0,
            "summary": trend_diag.get("summary") or {},
            "attempts": trend_diag.get("attempts") or [],
            "fetchError": trend_diag.get("fetchError") or "",
            "error": err,
            "kvWrites": writes.records[trend_mark:],
        }

    if state is not None and state.trends_off:
        trend_err = "本批已禁用趋势（源不可用）"
        _tool_step(
            "trend", lambda: (False, trend_err),
            inputs={"term": term, "key": key, "skipped": True,
                    "reason": trend_err},
            outputs=lambda res: {"skipped": True, "reason": res[1],
                                 "kvWrites": []},
            parent=parent_run, term=term,
        )
    else:
        try:
            trend_ok, trend_err = _tool_step(
                "trend", lambda: _trend(term, kv, writes, trend_diag),
                inputs={"term": term, "key": key, "cacheKey": TREND_PREFIX + key},
                outputs=trend_outputs, parent=parent_run, term=term,
            )
        except SourceUnavailable as exc:
            trend_err = exc.brief()
            if state is not None and state.disable_trends():
                log.warning("本批禁用趋势：%s", exc.brief())
    out["trend"] = trend_ok

    # 补数据缺一块不等于这条分析失败（AI 已经给出结论了），只记 notice 供前端提示。
    notes = []
    if serp_err:
        notes.append("SERP 未取到：" + serp_err)
    if trend_err:
        notes.append("趋势未取到：" + trend_err)
    if notes:
        out["notice"] = "；".join(notes)

    brief = serp_brief(serp_data)
    signal = work_signal(brief)
    try:
        text, model = _llm(build_messages(dict(item, serp=brief)))
    except LLMEmptyContent as exc:
        # 「调用成功但没正文」≠「调用异常」：错误说明必须来自真实诊断，
        # 不能笼统写成「LLM_API_KEY 未配置」，也不影响其它词。
        out["error"] = str(exc)
        out["llm_diag"] = exc.diag
        return _finalize(out, writes)
    except Exception as exc:  # noqa: BLE001
        out["error"] = "LLM 调用失败：" + str(exc)[:160]
        if state is not None:
            n = state.note_llm_failure()
            log.warning("LLM 失败（本批第 %d 次，继续跑其它词）：%s", n, str(exc)[:120])
        return _finalize(out, writes)
    if not text:
        # 兜底：_llm 正常会抛 LLMEmptyContent；若某个注入实现仍返回空串，
        # 也绝不把空正文写进 KV，且不臆断成「key 没配」。
        out["error"] = "LLM 返回空正文（未携带诊断信息，请查 hotwords_llm_empty_content 日志）"
        return _finalize(out, writes)
    out["model"] = model

    verdict, reason = decide(text, signal)
    out["verdict"] = verdict
    hints = signals_of(text)

    # ---- 回写 KV：正文 / 标签 / 状态，整段是一条 tool:kv.write span ----
    # ★正文先写：正文写失败就直接收手，绝不写状态——否则会留下「有状态、没正文」的悬空词，
    #   _select_pending 既不会重跑它，前端也看不到方案。
    writes.phase = "analysis"
    write_mark = len(writes.records)
    stopped = {"afterTextFailure": False}

    def write_back():
        if not writes.text(kv, AI_CACHE_PREFIX + key, text):
            stopped["afterTextFailure"] = True
            return
        if hints is not None:
            writes.table(kv, HINTS_KEY,
                         lambda m: set_hint_override(m, term, hints), reader=_kv_dict)
        if verdict == "不做":
            payload = {"term": term, "reason": reason or "AI 判定不做", "source": "ai"}
            if writes.table(kv, REJECTED_KEY,
                            lambda arr: upsert_rejected(arr, payload), reader=_kv_list):
                out["status"] = "rejected"
        elif verdict == "做":
            cand = {k: item.get(k) for k in ("term", "related", "hints", "geos", "geoCount", "traffic", "count", "score")}
            payload = {**cand, "term": term, "status": "todo", "verdict": "做"}
            if writes.table(kv, SAVED_KEY,
                            lambda arr: upsert_saved(arr, payload), reader=_kv_list):
                out["status"] = "todo"
        # 「观望」与解析不出结论：只留正文和标签，状态保持未跟进

    def write_outputs(_result):
        return {
            "verdict": verdict, "status": out.get("status") or "",
            "textChars": len(text), "textPreview": text[:200],
            "hints": hints,
            "stoppedAfterTextFailure": stopped["afterTextFailure"],
            "writes": writes.records[write_mark:],
            "writeErrors": list(writes.failed),
        }

    _tool_step(
        "kv.write", write_back,
        inputs={"term": term, "key": key, "aiCacheKey": AI_CACHE_PREFIX + key,
                "verdict": verdict, "hints": hints,
                "tables": [HINTS_KEY, SAVED_KEY, REJECTED_KEY]},
        outputs=write_outputs, parent=parent_run, term=term,
    )

    return _finalize(out, writes)


# ---------------------------------------------------------------------------
# 批次 trace：graph 根 → 批次 span → 每词 span → ChatOpenAI（graph 结构本身不动）
# ---------------------------------------------------------------------------
def _batch_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """批次 span 的 outputs：LangSmith 上直接看成功数与降级情况，不用点开 items。"""
    keys = ("ok", "fetched", "analyzed", "failed", "successRate", "counts",
            "serpDisabled", "trendsDisabled", "llmFailures", "error", "note")
    return {k: out[k] for k in keys if k in out}


def _word_outputs(out: dict[str, Any]) -> dict[str, Any]:
    """逐词 span 的 outputs：结论、状态、补数情况与写入失败明细。"""
    keys = ("verdict", "status", "model", "serp", "trend", "notice",
            "writeErrors", "error", "llm_diag")
    return {k: out[k] for k in keys if k in out}


def _word_error(out: dict[str, Any]) -> str | None:
    """业务失败也让 LangSmith 标红：_analyze_one 不抛异常，trace 自己看不到失败。"""
    return out.get("error") or None


def _trace_run_info(run, batch_id: str) -> dict[str, Any]:
    """批次回链信息（附加字段，老前端忽略也不受影响）。

    只拿已配置的 LANGSMITH_PROJECT_URL 拼 URL，**不在这里发网络请求**；没配就留空，
    用 runId 在 LangSmith 搜索框里一样能定位这条根 trace。
    """
    base = settings.langsmith_project_url.strip().rstrip("/")
    return {
        "batchId": batch_id,
        "runId": str(run.id),
        "traceId": str(getattr(run, "trace_id", "") or run.id),
        "url": f"{base}/r/{run.id}?poll=true" if base else "",
    }


def run_batch(limit: int = 20, kv: Optional[CloudflareKV] = None) -> dict[str, Any]:
    """读 KV 候选池 → 并发逐条分析 → 汇总。KV 不可达时整批失败，不写任何状态。

    ★2026-11-13：不再有「整批中止」。每个词独立跑完并把自己的失败写进 items[].error；
      源级故障只把「取数」这一步降级（serpDisabled / trendsDisabled），其它词照常出结论。
      整表写入都在 kv.atomic_update 的进程级 RLock 里完成，并发不会互相覆盖。

    ★trace：整批一条 span（自动挂到 LangGraph 根下），每个词一条子 span，词里的
      ChatOpenAI 再挂在词 span 下 —— Traces 页面展开一条就能看完 20 个词，而不是
      散落 20 条孤立根 run。父 run 显式传，绕开 worker 线程的 contextvars 丢失。
    """
    batch_id = f"hotwords-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"

    def body(batch_run):
        out = _run_batch(limit, kv, batch_run, batch_id)
        # 放在包装层：KV 读失败 / 无候选这些提前返回也带同一条根 run 的回链
        if batch_run is not None:
            out["traceRun"] = _trace_run_info(batch_run, batch_id)
        return out

    return run_traced(
        body,
        name=BATCH_TRACE_NAME,
        inputs={"limit": limit, "batchId": batch_id},
        tags=["site-agent", "hotword-batch"],
        metadata={"batch_id": batch_id, "thread_id": batch_id,
                  "component": "analyze_hotwords"},
        outputs=_batch_outputs,
        pass_run=True,
    )


def _run_batch(limit: int, kv: Optional[CloudflareKV], batch_run,
               batch_id: str) -> dict[str, Any]:
    """run_batch 的执行体；batch_run 是逐词 span 的显式父 run（关闭追踪时为 None）。"""
    kv = kv or get_kv()
    limit = max(1, min(int(limit or 20), 50))
    try:
        # 候选池读取也是一次 IO：KV 挂了/形状漂移会在 tool:kv.read-candidates 上标红，
        # 正常时能看到「筛出哪几个词」，不用再去翻日志猜为什么这批跑空了。
        items = trace_tool(
            lambda: _select_pending(kv, limit),
            name=TOOL_TRACE_PREFIX + "kv.read-candidates",
            parent=batch_run,
            inputs={"limit": limit,
                    "keys": ["candidates", SAVED_KEY, REJECTED_KEY, TAGS_KEY,
                             AI_CACHE_PREFIX]},
            tags=["site-agent", "hotword-tool"],
            metadata={"batch_id": batch_id, "component": "analyze_hotwords"},
            outputs=lambda res: {
                "selected": len(res),
                "terms": [x.get("term", "") for x in res],
                "keys": [x.get("key", "") for x in res],
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("候选池读取失败：%s", exc)
        return {"ok": False, "error": "候选池读取失败：" + str(exc)[:200],
                "items": [], "fetched": 0, "analyzed": 0, "failed": 0, "counts": {}}

    if not items:
        return {"ok": True, "items": [], "fetched": 0, "analyzed": 0,
                "failed": 0, "counts": {}, "note": "没有未跟进且未分析过的候选词"}

    state = _BatchAbort()

    def work(pair: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        idx, item = pair
        term = str(item.get("term") or "").strip()
        key = term_key(item.get("key") or term)
        return run_traced(
            # ★pass_run：把本词 span 的 run 交给 _analyze_one，让它成为该词所有
            #   tool:* 子 span 的显式父（worker 线程拿不到主线程的 contextvar）。
            lambda word_run: _analyze_one(item, kv, state, word_run),
            name=WORD_TRACE_PREFIX + (term or key)[:60],
            parent=batch_run,          # ★显式父：worker 线程没有主线程的 contextvar
            inputs={"term": term, "key": key, "index": idx},
            tags=["site-agent", "hotword"],
            metadata={
                "batch_id": batch_id,
                # 整批共用一个 thread_id：Threads 视图也聚成一条，不把 20 个词拆散。
                "thread_id": batch_id,
                # 每词独立标识；要单独看某个词，按 word_id 检索即可。
                "word_id": f"{batch_id}/{idx}/{key}",
                "term": term, "key": key, "index": idx,
            },
            outputs=_word_outputs,
            error=_word_error,
            pass_run=True,
        )

    results: list[dict[str, Any]] = []
    # 按 chunk 提交：一次最多 BATCH_WORKERS 条在飞，外部 LLM/SERP 超时不会把 20 条全排进队列。
    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        for i in range(0, len(items), BATCH_WORKERS):
            chunk = list(enumerate(items[i:i + BATCH_WORKERS], start=i))
            results.extend(pool.map(work, chunk))

    counts: dict[str, int] = {}
    for r in results:
        key = r["verdict"] or "未定"
        counts[key] = counts.get(key, 0) + 1
    analyzed = sum(1 for r in results if not r["error"])
    failed = len(results) - analyzed
    # 整批口径按成功率判定，并把比例一并回传，前端可直接展示。
    rate = analyzed / len(results) if results else 0.0
    out = {
        "ok": rate >= BATCH_OK_MIN_RATE,
        "fetched": len(items),
        "analyzed": analyzed,
        "failed": failed,
        "successRate": round(rate, 3),
        "counts": counts,
        "items": results,
        "serpDisabled": state.serp_off,
        "trendsDisabled": state.trends_off,
        "llmFailures": state.llm_failures,
    }
    degraded = []
    if state.serp_off:
        degraded.append("SERP 源不可用，本批已跳过 SERP（结论缺少辨形线索）")
    if state.trends_off:
        degraded.append("趋势源不可用，本批已禁用趋势")
    if degraded:
        out["note"] = "；".join(degraded) + "；其余词仍照常分析并回写"
    return out


# ---------------------------------------------------------------------------
# 状态回填：把「正文已写、状态表丢了」的词补回去（否则永远不会被重跑，也看不见）
# ---------------------------------------------------------------------------
def backfill_states(kv: Optional[CloudflareKV] = None, *, limit: int = 200,
                    dry_run: bool = False) -> dict[str, Any]:
    """从已落库的 `ai:v3:<key>` 正文反推结论，补写缺失的 saved / rejected / aihints。

    触发场景：正文写成功、但 5 张整表里某次 put_json 失败（或历史上并发 lost update），
    于是 `_select_pending` 因「已有正文」永久剔除该词，saved/rejected 里却查不到它——
    既不会重跑、前端也看不见。这里重解析正文第 7/8 节，**不调 LLM、不重抓 SERP/Trends**，
    把状态与标签按同一套 `upsert_*` / `set_hint_override` 口径补回去。

    dry_run=True 只出报告不写，建议先跑一次核对。结论「观望」按设计不写状态（只补标签）；
    正文为空 / 结论解析不出且状态缺失的词会列进 items（action=unresolved）——要重跑它们
    得先删掉对应的 `ai:v3:` 正文，因为 _select_pending 只认正文存在与否。
    """
    kv = kv or get_kv()
    limit = max(1, min(int(limit or 200), 1000))
    report: dict[str, Any] = {
        "ok": True, "dryRun": bool(dry_run), "scanned": 0, "repaired": 0,
        "hintsRepaired": 0, "alreadyCurated": 0, "unresolved": 0,
        "writeErrors": [], "items": [],
    }
    try:
        cache_keys = [k for k in kv.list_keys(AI_CACHE_PREFIX)
                      if k.startswith(AI_CACHE_PREFIX)]
        saved = _kv_list(kv, SAVED_KEY)
        rejected = _kv_list(kv, REJECTED_KEY)
        tags = _kv_dict(kv, TAGS_KEY)
        hints_map = _kv_dict(kv, HINTS_KEY)
        cand_map: dict[str, dict[str, Any]] = {}
        for c in _kv_list(kv, "candidates"):
            if isinstance(c, dict) and c.get("term"):
                cand_map[term_key(c.get("key") or c["term"])] = c
    except Exception as exc:  # noqa: BLE001
        log.warning("状态回填读取 KV 失败：%s", exc)
        report["ok"] = False
        report["error"] = "状态回填读取 KV 失败：" + str(exc)[:200]
        return report

    def curated_now() -> set[str]:
        """锁内复核：回填期间可能正好有一批分析写完状态，别覆盖人工/新结论。"""
        cur: set[str] = set()
        for row in _kv_list(kv, SAVED_KEY) + _kv_list(kv, REJECTED_KEY):
            if isinstance(row, dict) and row.get("key"):
                cur.add(term_key(row["key"]))
        cur.update(term_key(k) for k, v in _kv_dict(kv, TAGS_KEY).items() if v)
        return cur

    curated: set[str] = set()
    for row in saved + rejected:
        if isinstance(row, dict) and row.get("key"):
            curated.add(term_key(row["key"]))
    curated.update(term_key(k) for k, v in tags.items() if v)

    for cache_key in cache_keys[:limit]:
        key = cache_key[len(AI_CACHE_PREFIX):]
        if not key:
            continue
        report["scanned"] += 1
        text = kv.get_text(cache_key) or ""
        if not text.strip():
            report["unresolved"] += 1
            report["items"].append({"key": key, "action": "unresolved", "reason": "正文为空"})
            continue

        verdict, reason = decide(text)
        hints = signals_of(text)
        need_state = key not in curated and verdict in ("做", "不做")
        # 「信号标签：无」解析成 []，写成空覆盖没有意义，回填时跳过
        need_hints = bool(hints) and key not in hints_map

        if not need_state and not need_hints:
            # 结论「观望」本来就没有状态，和「已整理」一样不算缺口；
            # 只有结论解析不出、状态也没补上的才算真卡住。
            if verdict:
                report["alreadyCurated"] += 1
            else:
                report["unresolved"] += 1
                report["items"].append({
                    "key": key, "action": "unresolved",
                    "reason": "正文解析不出结论且状态缺失；要重跑需先删除该词的 ai:v3: 正文",
                })
            continue

        action = "repaired" if not dry_run else "preview"
        entry: dict[str, Any] = {
            "key": key, "action": action, "verdict": verdict or "未定",
            "state": verdict if need_state else "", "hints": bool(need_hints),
        }
        report["items"].append(entry)
        if dry_run:
            continue

        writes = _WriteLog(key)
        cand = cand_map.get(key, {})
        # 与正在跑的 batch 共用同一把 RLock（可重入）：状态 + 标签一起改完再放开，
        # 避免回填到一半被另一个词插进来读到半套数据。
        with table_lock():
            if need_state and key in curated_now():
                # 回填扫描期间刚被整理过 → 不再覆盖，保证幂等、不抢人工标记
                need_state = False
                report["alreadyCurated"] += 1
                entry["state"] = ""
            need_hints = bool(hints) and key not in _kv_dict(kv, HINTS_KEY)
            entry["hints"] = bool(need_hints)
            if not need_state and not need_hints:
                entry["action"] = "skipped"
            if need_state and verdict == "不做":
                payload = {"term": cand.get("term") or key,
                           "reason": reason or "AI 判定不做", "source": "ai"}
                if writes.table(kv, REJECTED_KEY,
                                lambda arr: upsert_rejected(arr, payload), reader=_kv_list):
                    report["repaired"] += 1
            elif need_state and verdict == "做":
                fields = ("term", "related", "hints", "geos", "geoCount", "traffic", "count", "score")
                payload = {**{f: cand.get(f) for f in fields},
                           "term": cand.get("term") or key, "status": "todo", "verdict": "做"}
                if writes.table(kv, SAVED_KEY,
                                lambda arr: upsert_saved(arr, payload), reader=_kv_list):
                    report["repaired"] += 1
            if need_hints:
                if writes.table(kv, HINTS_KEY,
                                lambda m: set_hint_override(m, key, hints), reader=_kv_dict):
                    report["hintsRepaired"] += 1
        report["writeErrors"].extend(writes.failed)

    if report["writeErrors"]:
        report["ok"] = False
        report["error"] = "部分状态回填写入失败：" + "、".join(dict.fromkeys(report["writeErrors"]))
    return report
