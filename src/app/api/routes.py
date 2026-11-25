# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""HTTP 入站：每个场景一个入口，入口之间不互相分流。

    POST /run              常规线主图（意图分类 → 取数 → 校验 → 输出）
    POST /hotwords         发起热词循环图（立即返回，后台线程逐词跑）
    GET  /hotwords/status  热词线最近一轮的进度（给 /agent 页 3 秒轮询）
    POST /page             建页子图（取词 → 归类 → 渲染 → 推送 → 验活）
    GET  /health           状态与进程内计数
    GET  /llm-quota        DeepSeek 余额与用量

入口层只做 HTTP 绑定：请求模型 → graph 层的 run_*()。不构造 state、不驱动图、
不拼 trace——两条线的构造与触发机制在 graph/ 里完全一致（normal_graph / hotword_graph），
差别只在节点类型与 state 内容。热词线的 IO 与常规线走同一个工具注册表，
区别只是它按**步骤名单**逐词循环调用（名单 → 节点的连线在 graph/hotword_graph.py）。
热词线是唯一异步入口：逐词串行要 5-10 分钟，同步跑会把 console 代理拖超时。
"""

from __future__ import annotations

import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.app.config import settings
from src.app.graph import run_hotwords, run_page_build, run_workflow
from src.app.graph.state import HOTWORD_LIMIT_MAX
from src.app.llm.quota import check_quota
from src.app.observability.hotword_progress import hotword_progress
from src.app.observability.metrics import metrics
from src.app.utils.logger import get_logger

log = get_logger(__name__)


class WorkflowRequest(BaseModel):
    input_text: str
    intent: str | None = None
    tools: list[str] | None = None
    tool_args: dict[str, dict] | None = None   # 点名工具的入参，如 {"launch_new_site": {"name": "x"}}
    prompt_versions: dict[str, str] | None = None
    thread_id: str | None = None


class HotwordsRequest(BaseModel):
    limit: int = HOTWORD_LIMIT_MAX
    prompt_version: str | None = None
    evidence: list[str] | None = None    # 本轮补证据的步骤名单（None=默认）
    post: list[str] | None = None        # 本轮后置动作的步骤名单（None=默认）


api = FastAPI(title=settings.project_name)


@api.post("/run")
def run(request: WorkflowRequest) -> dict:
    """常规线主图：请求字段与 run_workflow 签名一一对应，直接展开。"""
    return run_workflow(**request.model_dump())


def _run_hotwords_bg(**kwargs) -> None:
    """后台线程跑一轮；入口已把进度置 running，成功/异常都在这里收口。"""
    try:
        summary = run_hotwords(**kwargs)
    except Exception as exc:     # noqa: BLE001 —— 后台线程不能静默死掉
        log.exception("候选池分析后台轮次异常退出")
        hotword_progress.fail(str(exc) or exc.__class__.__name__)
        return
    hotword_progress.finish(summary)


@api.post("/hotwords")
def hotwords(request: HotwordsRequest) -> dict:
    """发起一轮候选池分析：立即返回 {started, limit}，进度走 GET /hotwords/status。

    逐词串行要 5-10 分钟，同步跑会把 console 代理拖超时、也扛不住实例重启；
    这里起守护线程跑图，页面每 3 秒轮询一次进度。同一时刻只允许一轮（在跑 → 409）。
    evidence / post 只能取步骤白名单里的名字（serp / trend / write_result），
    不是任意工具名——这个入口不带强鉴权，不能让它变成不可逆写工具的旁路；
    名单非法时整轮 ok=False，不跑任何词。
    """
    limit = max(1, min(int(request.limit or HOTWORD_LIMIT_MAX), HOTWORD_LIMIT_MAX))
    if not hotword_progress.start(limit):
        raise HTTPException(status_code=409,
                            detail="已有一轮候选池分析在跑，先看进度、等它结束再发起")
    threading.Thread(target=_run_hotwords_bg, daemon=True,
                     kwargs={**request.model_dump(), "limit": limit}).start()
    return {"ok": True, "started": True, "limit": limit}


@api.get("/hotwords/status")
def hotwords_status() -> dict:
    """最近一轮热词分析的进度；进程重启后回到 idle（结果仍在 KV 的 AI 缓存里）。"""
    return hotword_progress.snapshot()


class PageRequest(BaseModel):
    term: str = ""                        # 空 = 自动取下一条带分析正文的 todo 词
    confirm: bool = False                 # False = dry-run 只预览，不写任何数据
    planned_site: str = ""                # 排产台账给的目标站；空 = 由建页线现判
    mode: str = "auto"                    # auto=自动校验判重；enhance=显式增强已有页（绕过同页拦截）


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
