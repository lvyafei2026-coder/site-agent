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
  热词线  fetch_one → analyze_one → route_after_analyze ─┬─ fetch_one（回环）
                                                        └─ build_summary（收尾）
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from src.app.config import settings
from src.app.services.hotword_service import _DegradeState


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
    三个节点各取自己那份；缺省时回退 settings.default_prompt_versions（D1 无默认再回退此值）。"""

    requested_tools: list[str] | None
    """可选输入。显式指定本轮调用的工具名列表，覆盖 INTENT_TOOLS 默认集。
    None = 按意图默认集（只含 read 工具）；显式传 write 工具名（如 git_push）才会触发不可逆写——这是当前唯一的写工具开关。"""

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
    prompt_versions: dict[str, str] | None = None,
) -> WorkflowState:
    """构造常规线一轮的入口状态（与 initial_hotword_state 对称）。"""
    state: WorkflowState = {
        "input_text": input_text,
        "retry_count": 0,
        "messages": [],
        "prompt_versions": dict(prompt_versions or settings.default_prompt_versions),
        "tool_results": {},
        "requested_tools": tools,
    }
    if intent:
        state["intent"] = intent
    return state


class HotwordLoopState(TypedDict, total=False):
    """热词循环图在节点间流转的状态。字段全为名词，避免与节点名撞名。

    与 WorkflowState 的差别只在内容：这里带本轮的降级状态（degrade）与逐词明细，
    所以对外返回体取终态的 `summary` 字段，而不是整份 state。
    KV 句柄不进 state：工具自己取 get_kv()，两条线一致。
    """

    # ——入口参数（initial_hotword_state 注入，节点只读）——
    limit: int                       # 本次最多分析的词数（= 最大循环次数）
    prompt_version: Optional[str]    # 本轮提示词版本（None → 节点侧回退默认）
    degrade: _DegradeState           # 源级降级状态（SERP/Trends/LLM）

    # ——节点间流转——
    seen: set[str]                   # 本轮已取过的 key（防 KV 最终一致导致重复取词）
    examples_loaded: bool            # 人工判例是否已读（只读一次）
    examples: list[dict[str, str]]
    has_more: bool                   # fetch_one 本轮是否取到词（False → 条件边收尾）
    current: dict[str, Any]          # 本轮取出的待分析词
    results: list[dict[str, Any]]
    loop_count: int
    load_error: str                  # 候选池读取失败信息（非空 → 直接收尾）

    # ——输出（build_summary 节点产出，运行入口直接取用）——
    summary: dict[str, Any]


def initial_hotword_state(limit: int = 20,
                          prompt_version: Optional[str] = None) -> HotwordLoopState:
    """构造热词线一轮的入口状态（与 initial_workflow_state 对称）。

    limit 是最大循环次数，收口在 1-50。prompt_version 原样带上（可为 None）：
    默认版本由 prompts.registry 结合 D1 的默认版本决定，不在这里提前拍死。
    """
    return {
        "limit": max(1, min(int(limit or 20), 50)),
        "prompt_version": prompt_version,
        "degrade": _DegradeState(),
        "seen": set(),
        "examples_loaded": False,
        "examples": [],
        "has_more": False,
        "current": None,
        "results": [],
        "loop_count": 0,
    }
