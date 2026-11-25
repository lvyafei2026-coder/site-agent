# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""HTTP 入站：每个场景一个入口，入口之间不互相分流。

    POST /run        常规线主图（意图分类 → 取数 → 校验 → 输出）
    POST /hotwords   热词循环图（逐词取词 → 分析 → 汇总）
    POST /page       建页子图（取词 → 归类 → 渲染 → 推送 → 验活）
    GET  /health     状态与进程内计数
    GET  /llm-quota  DeepSeek 余额与用量

入口层只做 HTTP 绑定：请求模型 → graph 层的 run_*()。不构造 state、不驱动图、
不拼 trace——两条线的构造与触发机制在 graph/ 里完全一致（normal_graph / hotword_graph），
差别只在节点类型与 state 内容。热词线的 IO 与常规线走同一个工具注册表，
区别只是它按**步骤名单**逐词循环调用（名单 → 节点的连线在 graph/hotword_graph.py）。
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from src.app.config import settings
from src.app.graph import run_hotwords, run_page_build, run_workflow
from src.app.llm.quota import check_quota
from src.app.observability.metrics import metrics


class WorkflowRequest(BaseModel):
    input_text: str
    intent: str | None = None
    tools: list[str] | None = None
    tool_args: dict[str, dict] | None = None   # 点名工具的入参，如 {"launch_new_site": {"name": "x"}}
    prompt_versions: dict[str, str] | None = None
    thread_id: str | None = None


class HotwordsRequest(BaseModel):
    limit: int = 20
    prompt_version: str | None = None
    evidence: list[str] | None = None    # 本轮补证据的步骤名单（None=默认）
    post: list[str] | None = None        # 本轮后置动作的步骤名单（None=默认）


api = FastAPI(title=settings.project_name)


@api.post("/run")
def run(request: WorkflowRequest) -> dict:
    """常规线主图：请求字段与 run_workflow 签名一一对应，直接展开。"""
    return run_workflow(**request.model_dump())


@api.post("/hotwords")
def hotwords(request: HotwordsRequest) -> dict:
    """热词循环图：请求字段与 run_hotwords 签名一一对应，直接展开。

    返回图终态的汇总（ok / analyzed / failed / items / evidenceSteps …）+ trace + langsmith，
    与 /run 同一套封装；limit 最大 50，由 state 工厂收口。
    evidence / post 只能取步骤白名单里的名字（serp / trend / write_result），
    不是任意工具名——这个入口不带强鉴权，不能让它变成不可逆写工具的旁路；
    名单非法时整轮 ok=False，不跑任何词。
    """
    return run_hotwords(**request.model_dump())


class PageRequest(BaseModel):
    term: str = ""                        # 空 = 自动取下一条带分析正文的 todo 词
    confirm: bool = False                 # False = dry-run 只预览，不写任何数据
    planned_site: str = ""                # 排产台账给的目标站；空 = 由建页线现判


@api.post("/page")
def page(request: PageRequest) -> dict:
    """建页子图：请求字段与 run_page_build 签名一一对应，直接展开。

    与 /run、/hotwords 同一套封装（终态汇总 + trace + langsmith）。一次只建一页，
    逐词建页由调用方多次触发。confirm=False 是 dry-run：plan 出结构与目标站即收尾；
    confirm=True 才真正渲染、推送 GitHub 并验活——这个入口不带强鉴权，
    但写动作只发生在 confirm=true 时，且目标站点与页面结构都来自已有分析正文，不接受任意输入。
    """
    return run_page_build(**request.model_dump())


@api.get("/health")
def health() -> dict:
    return {"status": "ok", "metrics": metrics.snapshot()}


@api.get("/llm-quota")
def llm_quota() -> dict:
    """DeepSeek 余额与用量：官方 /user/balance 零消耗查询 + 可选的平台用量面板。

    配了 DEEPSEEK_PLATFORM_TOKEN 才查近 30 天消费 / 请求次数 / Tokens / 累计消费；
    未配时 usage.ok=False 并给出提示，余额照常返回。只在页面手动点击时触发。
    """
    return check_quota()
