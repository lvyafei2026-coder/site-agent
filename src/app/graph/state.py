# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""两条线的 State 与入口状态工厂（并列、同构）。

    WorkflowState      + initial_workflow_state()   常规线主图（POST /run）
    HotwordLoopState   + initial_hotword_state()    热词循环图（POST /hotwords）

字段分三组：
- 输入：由运行入口注入（graph/normal_graph.py、graph/hotword_graph.py），节点只读不改；
- 中间：节点产出、供下游节点消费，最后随结果一并返回；
- 输出：终态字段，运行入口的返回体直接取用。

两条线的走向（见 graph/normal_graph.py、graph/hotword_graph.py）：
  常规线  classify_intent → run_processing → validate_result → build_output / retry / handle_error
  热词线  fetch_one → analyze_word → 回环 ┬ fetch_one（还有词）
                                         └ build_summary（收尾）
          analyze_word 是按本轮名单编译的**逐词子图**：补证据节点 → LLM → 后置动作节点 → 落账。
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from src.app.config import settings


class WorkflowState(TypedDict, total=False):
    # ── 输入（运行入口注入，节点只读）───────────────────────────
    input_text: str
    """必填。原始请求文本；intent 节点拿它做意图分类，processing 节点拿它做字段抽取。"""

    intent: str
    """可选输入 / 中间变量（双重身份）。
    传入时（initial_workflow_state 写入）= 调用方强制指定意图，intent 节点跳过 LLM 直接采用、confidence 置 1.0；
    未传入时 = intent 节点产出的中间结果，供 processing 选工具集（INTENT_TOOLS）。"""

    prompt_versions: dict[str, str]
    """可选输入。按任务指定 prompt 版本，形如 {"intent": "v1", "extraction": "v1", "hotwords": "v2"}；
    三个节点各取自己那份。**只放调用方真正指定的任务**，缺省留空——空即「用 D1 默认版本」
    （registry 再回退 settings.default_prompt_versions）。不要把 config 的兜底表整体灌进来。"""

    requested_tools: list[str] | None
    """可选输入。显式指定本轮调用的工具名列表，覆盖 INTENT_TOOLS 默认集。
    None = 按意图默认集（只含 read 工具）；显式传 write 工具名（如 git_push）才会触发不可逆写——这是当前唯一的写工具开关。"""

    tool_args: dict[str, dict]
    """可选输入。点名工具的入参，形如 {"launch_new_site": {"name": "invoices"}}；
    没点名的工具忽略此项，仍走 TOOL_ARGS 预置。缺省为空 dict。"""

    # ── 中间变量（节点产出，节点间流动）───────────────────────────
    confidence: float
    """intent 节点产出。意图分类置信度 0~1（强制指定时为 1.0，LLM 失败回退 0.55）；
    validation 打分（confidence * 0.4）和最终 WorkflowResult 都用它。"""

    extracted_data: dict[str, Any]
    """processing 节点产出。LLM 抽取的运营字段（target_site/keywords/pages/notes 等）+ analysis 汇总；
    validation 节点以它做字段覆盖率校验。"""

    metrics_analysis: str
    """processing 节点产出（review_metrics 专属）。LLM 对各站埋点 / GSC / 页面数据做的自然语言分析，
    识别「表现好的站点」及理由；无 LLM key 或分析失败时为空串。build_output 把它并入 output 报告。"""

    metrics_notice: dict
    """processing 节点产出（review_metrics 专属）。分析正文推给运营者的邮件回执：
    {sent, subject, [detail]}；sent=False 只表示没发出去（缺凭据/失败），不影响整轮状态。"""

    outreach_result: dict
    """processing 节点产出（find_backlinks 专属）。外链起稿 + Dev.to 发布回执：
    {ok, article:{title,tags,target_url,draft}, status, detail, source_url}；
    ok=False 时 status 说明败在哪一步（draft_failed / blocked / failed）。"""

    planning_analysis: str
    """processing 节点产出（word_plan_page 专属）。LLM 排产方案（markdown）：
    待开发池 × 近 30 天新页 × 历史复盘 → 选哪些词 / 落哪个站 / 排除原因；
    无 LLM key 或排产失败时为空串。build_output 把它并入 output 报告。"""

    promotion_analysis: str
    """processing 节点产出（plan_promo 专属）。确定性推广排期报告（markdown）：
    近 30 天已上线页 × 上版排期去重 → 日期/渠道/目标链接，并已落 agent_reports(kind='plan')。
    无已上线页或排期失败时为空串。build_output 把它并入 output 报告。"""

    tool_results: dict[str, Any]
    """processing 节点产出。{工具名: ToolOutput dict}，入口初始化为 {}；
    validation 节点据此判断「全部失败不算 is_valid」，WorkflowResult 统计成功/失败数。"""

    validation: dict[str, Any]
    """validation 节点产出。{is_valid, reason, score, tool_failures}；
    graph/edges.py 据此路由：is_valid → build_output，否则 retry_count < 上限 → retry，再不行 → handle_error。"""

    retry_count: int
    """循环控制。入口置 0，processing 节点每执行一轮 +1；
    edges 与 settings.max_validation_retries（默认 2）比较，决定还允许重试几次。"""

    messages: list[str]
    """过程轨迹。入口置 []，各节点追加标记（intent:xxx / processed / validated）；
    无节点消费，仅随最终结果返回，用于排查一轮里实际走了哪些节点。"""

    # ── 输出（终态，/run 返回体直接取用）───────────────────────────
    output: str
    """终态文本。build_output 节点写入 WorkflowResult.render() 的运营报告；
    handle_error 节点写入 "ERROR: <原因>"。/run 返回体里的 output 字段就是它。"""

    error: str
    """终态错误信息。handle_error 节点写入失败原因（优先取 validation["reason"]）；
    edges 兜底：validate_result 之后若 error 非空，直接路由到 handle_error。"""

    processing_fatal: bool
    """processing 节点级硬失败信号（LLM 已配置但调用挂了 / 工具执行抛未捕获异常）。
    只在确实硬失败时写入 True；graph 的 route_after_processing 据此**跳过校验与重试**，
    直接路由到 handle_error。软失败（工具 ok:False、字段偏少）不写此字段。"""


def initial_workflow_state(
    input_text: str,
    *,
    intent: str | None = None,
    tools: list[str] | None = None,
    tool_args: dict[str, dict] | None = None,
    prompt_versions: dict[str, str] | None = None,
) -> WorkflowState:
    """构造常规线一轮的入口状态（与 initial_hotword_state 对称）。"""
    state: WorkflowState = {
        "input_text": input_text,
        "retry_count": 0,
        "messages": [],
        # ★不要在这里预填 settings.default_prompt_versions：一旦每个 task 都塞了版本号，
        #   load_prompt 就把它当成「显式指定」，D1 prompt_defaults 里切换的默认版本永远
        #   读不到（排产因此长期跑在要求 markdown 的 v1 上，落库直接断）。空 dict 表示
        #   「本轮没指定」，由 registry 走「D1 默认 → config 兜底」。
        "prompt_versions": dict(prompt_versions or {}),
        "tool_results": {},
        "requested_tools": tools,
        "tool_args": dict(tool_args or {}),
    }
    if intent:
        state["intent"] = intent
    return state


class DegradeState:
    """热词线一轮的**降级状态**：哪些证据步骤本轮已关掉，外加 LLM 失败次数。

    单词级失败隔离后这里只保留「省时间 / 供汇总」两件事，不拦任何词：

      ・某步源不可用 → disabled.add(key)：后续词跳过这一步（省掉每词几发超时），照常走 LLM；
      ・LLM 失败      → 只累加次数供汇总，不中止。

    源级故障是「出口 IP 不通」这类全局问题，让一轮 100 个词各撞一遍超时毫无意义；
    跳过的是「取数」那一个节点，不是整词分析。

    key 就是步骤名（与 GraphNode 连线用的名单同一套），谁 disable 谁负责，不在这里表驱动。
    热词图逐词串行（无常驻线程），状态只在本轮内单向推进，无需加锁。
    """

    def __init__(self) -> None:
        self.disabled: set[str] = set()
        self.llm_failures = 0

    def disable(self, step: str) -> bool:
        """关掉一个证据步骤；返回是否首次置位（用于只打一条日志）。"""
        first = step not in self.disabled
        self.disabled.add(step)
        return first

    def note_llm_failure(self) -> int:
        """记一次 LLM 失败，返回累计次数。只用于日志/汇总。"""
        self.llm_failures += 1
        return self.llm_failures


class HotwordLoopState(TypedDict, total=False):
    """热词循环图在节点间流转的状态。字段全为名词，避免与节点名撞名。

    与 WorkflowState 的差别只在内容：这条线的「编排」就是图的连线，所以 state 里带
    本轮名单（evidence / post，由 graph/hotword_graph.py 解析校验）、降级状态和逐词通道。
    KV 句柄不进 state：工具自己取 get_kv()，两条线一致。
    """

    # ——入口参数（initial_hotword_state 注入，节点只读）——
    limit: int                       # 本次最多分析的词数（= 最大循环次数）
    prompt_version: Optional[str]    # 本轮提示词版本（None → 节点侧回退默认）
    degrade: DegradeState            # 源级降级状态（已关掉的步骤名 + LLM 失败次数）
    evidence: tuple[str, ...]        # 本轮 LLM 之前的补证据步骤名（= 子图里连了哪几个 gather）
    post: tuple[str, ...]            # 本轮 LLM 之后的后置动作步骤名

    # ——节点间流转——
    seen: set[str]                   # 本轮已取过的 key（防 KV 最终一致导致重复取词）
    examples_loaded: bool            # 人工判例是否已读（只读一次）
    examples: list[dict[str, str]]
    has_more: bool                   # fetch_one 本轮是否取到词（False → 条件边收尾）
    current: dict[str, Any]          # 本轮取出的候选词（KV 里的原始条目）
    word: dict[str, Any]             # 该词的结果骨架，步骤节点逐步填，collect_word 落账
    body: dict[str, Any]             # 提示词素材：current + 各证据节点写进来的部分
    signals: list[str]               # 硬证据（能把结论压成「不做」），decide 按顺序取
    ai: dict[str, Any]               # LLM 产出：正文 / 结论 / 理由 / 标签，给后置动作节点用
    results: list[dict[str, Any]]
    loop_count: int
    load_error: str                  # 本轮开跑前就失败的原因（名单非法 / 候选池读取失败）

    # ——输出（build_summary 节点产出，运行入口直接取用）——
    summary: dict[str, Any]


HOTWORD_LIMIT_MAX = 100   # 一轮最多逐词分析多少个；入口模型默认值与 state 收口共用


def initial_hotword_state(limit: int = 100,
                          prompt_version: Optional[str] = None,
                          evidence: tuple[str, ...] = (),
                          post: tuple[str, ...] = ()) -> HotwordLoopState:
    """构造热词线一轮的入口状态（与 initial_workflow_state 对称）。

    limit 是最大循环次数，收口在 1-100。prompt_version 原样带上（可为 None）：
    默认版本由 prompts.registry 结合 D1 的默认版本决定，不在这里提前拍死。
    evidence / post 是**已解析校验过的步骤名单**——名单与节点的对应关系在
    graph/hotword_graph.py（那里同时决定图怎么连线），这里只负责把通道初始化好。
    """
    return {
        "limit": max(1, min(int(limit or HOTWORD_LIMIT_MAX), HOTWORD_LIMIT_MAX)),
        "prompt_version": prompt_version,
        "degrade": DegradeState(),
        "evidence": evidence,
        "post": post,
        "seen": set(),
        "examples_loaded": False,
        "examples": [],
        "has_more": False,
        "current": None,
        "word": {},
        "body": {},
        "signals": [],
        "ai": {},
        "results": [],
        "loop_count": 0,
    }


# ============================================================================
# 建页线（POST /page）：一条待开发词 → 一页上线。与上面两线并列同构。
# ============================================================================

class PageBuildState(TypedDict, total=False):
    """建页子图在节点间流转的状态。一次只建一页（词级循环由调用方多次触发）。

    plan 节点产出 term / ai_text / site / page，后续节点逐步填充 work / sha /
    url / live_ok；error 短路后由 run_page_build 汇总成 summary。
    """

    # ── 输入（运行入口注入）──
    term: str                # 要建的词；空串 = 自动取下一条带正文的 todo 词
    confirm: bool            # False = dry-run（只 plan，不渲染不推送）
    planned_site: str        # 排产台账给的目标站；空串 = 让 plan 节点现判
    mode: str                # auto=自动判重；enhance=显式增强已有页（人工确认后）
    status_filter: str       # 取词的跟进状态，默认 todo

    # ── 节点间流转 ──
    picked: dict             # pick_word 的产出：{term, text, site?}（site 来自排产计划）
    ai_text: str             # 该词的 ai:v3 方案正文
    site: str                # 目标站点名（空串 = 归不进已有站）
    classify_by: str         # plan / llm / rule / none（汇总里如实展示）
    content_by: str          # llm / draft（正文来源：写出来的还是沿用分析稿）
    site_audit: dict         # 站情通读结果：add/enhance/skip + 匹配页 + 相似度
    site_digest: dict        # 原站页面清单 + 风格基调（供写作与渲染继承）
    page: dict               # 解析出的页面结构
    work: str                # render_files 返回的本地仓库路径
    sha: str                 # git_push 返回的 commit sha
    url: str                 # 验活命中的线上 URL
    live_ok: bool
    stage: str               # 走到了哪一步（summary 与失败定位用）
    error: str               # 第一个失败的步骤 + 原因

    # ── 输出（run_page_build 直接取用）──
    summary: dict[str, Any]


def initial_page_build_state(term: str = "", confirm: bool = False,
                             planned_site: str = "", mode: str = "auto",
                             status_filter: str = "todo") -> PageBuildState:
    """构造建页线一轮的入口状态（与前两个工厂对称）。

    planned_site 只在「调用方从排产台账点单」时给：那一行已经定过站点，
    plan 节点直接用，不再让 LLM 重判（排产与建页两边判出不同站就是事故）。
    """
    return {
        "term": str(term or ""),
        "confirm": bool(confirm),
        "planned_site": str(planned_site or ""),
        "mode": str(mode or "auto"),
        "status_filter": str(status_filter or "todo"),
        "picked": {},
        "ai_text": "",
        "content_by": "",
        "site_audit": {},
        "site_digest": {},
        "site": "",
        "classify_by": "",
        "page": {},
        "work": "",
        "sha": "",
        "url": "",
        "live_ok": False,
        "stage": "plan",
        "error": "",
        "summary": {},
    }
