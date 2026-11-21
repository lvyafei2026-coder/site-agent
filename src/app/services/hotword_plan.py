# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线的**步骤计划**：LLM 之前补哪些证据、之后做哪些后置动作，全声明在这里。

编排（hotword_service._analyze_one）只按计划遍历执行，函数体里不出现具体工具名：
    加一个证据源 = tools/hotwords_tools.py 加一个工具 + 这里加一条 Evidence；
    加一个后置动作 = 同理加一条 PostAction。循环、节点、图、请求模型都不用改。

计划按**名单**选择：run_hotwords(evidence=[...], post=[...])，/hotwords 请求体同名字段。
名单只能取本模块白名单里的步骤 key，不是任意 registry 工具名——这个入口不带强鉴权，
绝不能让它变成 git_push / send_notice 之类不可逆动作的旁路。未知 key 由 resolve_plan
抛 ValueError，入口把它收进 state.load_error，走与 KV 不可达同一条失败收口路径。

证据工具的统一返回形状（hotword_fetch.serp / trend）：
    {ok, data, error, writes, writeErrors, …}
ok=这一步有没有拿到数据，data=原始 payload（喂提示词前按 shape 变形），
writes/writeErrors=本次写入明细（编排层跨步骤汇总进该词 error）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.app.tools.adapters.hotwords.hotword_criteria import serp_brief, work_signal


def _identity(data: Any) -> Any:
    return data


def _bind_term(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"term": ctx["term"]}


@dataclass(frozen=True)
class Evidence:
    """LLM 之前的一步补证据：调哪个工具、怎么绑参、结果怎么喂进提示词。"""

    key: str                              # 词结果 / 降级 / 前端列的标识
    tool: str                             # registry 工具名
    label: str                            # 人读名（notice 与降级提示用）
    bind: Callable[[dict[str, Any]], dict[str, Any]] = _bind_term  # ctx → 工具入参
    feed: bool = True                     # 结果是否喂进提示词 body[key]
    shape: Callable[[Any], Any] = _identity   # data → body[key]
    signal: Optional[Callable[[Any], str]] = None  # 硬证据：能把结论压成「不做」


@dataclass(frozen=True)
class PostAction:
    """LLM 之后的一步后置动作：怎么绑参、结果怎么回填到词结果上。"""

    key: str
    tool: str
    label: str
    bind: Callable[[dict[str, Any]], dict[str, Any]]      # ctx → 工具入参
    apply: Callable[[dict[str, Any], dict[str, Any]], None]  # (词结果, result) → 回填


def _bind_write(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"item": ctx["item"], "text": ctx["text"], "verdict": ctx["verdict"],
            "reason": ctx["reason"], "hints": ctx["hints"]}


def _apply_write(out: dict[str, Any], result: dict[str, Any]) -> None:
    out["status"] = str(result.get("status") or "")


EVIDENCE: dict[str, Evidence] = {
    "serp": Evidence(key="serp", tool="fetch_serp", label="SERP",
                     shape=serp_brief, signal=work_signal),
    # 趋势只落库供词库页与后续判据用，不进提示词（feed=False）
    "trend": Evidence(key="trend", tool="fetch_trend", label="趋势", feed=False),
}

POST: dict[str, PostAction] = {
    "write_result": PostAction(key="write_result", tool="write_hotword_result",
                               label="结论回写", bind=_bind_write, apply=_apply_write),
}

DEFAULT_EVIDENCE = ("serp", "trend")
DEFAULT_POST = ("write_result",)

DEFAULT_EVIDENCE_STEPS = tuple(EVIDENCE[k] for k in DEFAULT_EVIDENCE)
DEFAULT_POST_STEPS = tuple(POST[k] for k in DEFAULT_POST)


def _resolve(names: Optional[list[str]], table: dict[str, Any],
             default: tuple[str, ...], what: str) -> tuple[Any, ...]:
    keys = list(default) if names is None else list(names)
    steps = []
    for key in keys:
        if key not in table:
            raise ValueError(f"未知{what}步骤：{key}（可选：{'、'.join(table)}）")
        steps.append(table[key])
    return tuple(steps)


def resolve_plan(evidence: Optional[list[str]] = None,
                 post: Optional[list[str]] = None) -> tuple[tuple[Evidence, ...],
                                                            tuple[PostAction, ...]]:
    """名单 → 步骤元组；None 用默认计划，空列表 = 这一步什么都不做。"""
    return (_resolve(evidence, EVIDENCE, DEFAULT_EVIDENCE, "证据"),
            _resolve(post, POST, DEFAULT_POST, "后置动作"))