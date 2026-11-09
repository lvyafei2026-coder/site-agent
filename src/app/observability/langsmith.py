# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LangSmith 追踪：唯一开关处。

三条原则：
  1. 开关只认 `settings`（.env / Render 环境变量），业务代码不直接读 LANGSMITH_*。
  2. `LANGSMITH_TRACING=true` **且** 有 `LANGSMITH_API_KEY` 才算开启——只有开关没
     有 key 时发送必然 401，会在日志里刷一堆噪音，不如直接当关闭。
  3. 关闭时零开销：`traceable` 原样返回函数，不 import langsmith 运行时。

开启后会自动产生三层 trace：
  - 图级：LangGraph 每次 `invoke` 一条 run（名字由调用方传 run_name）
  - 节点级：classify_intent / run_processing / validate_result / build_output …
  - LLM 级：`llm/provider.py` 用 `wrap_openai` 包过的客户端，请求体与返回值都在
"""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable

from src.app.config import settings
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def tracing_enabled() -> bool:
    """真开追踪的条件：开关为真 + 有 key。"""
    return bool(settings.langsmith_tracing and settings.langsmith_api_key)


def _resolve_project_url() -> str:
    """项目页地址：手填优先，没填就向 LangSmith 解析一次（成功缓存 1 小时）。

    失败一律返回空串——回链是锦上添花，不能因为 LangSmith 抽风就让 /run 变慢或报错。
    失败只缓存 5 分钟，避免每个请求都去试一次。
    """
    configured = settings.langsmith_project_url.strip()
    if configured:
        return configured
    if not tracing_enabled():
        return ""

    global _URL_CACHE
    url, at = _URL_CACHE
    ttl = 3600 if url else 300
    if time.monotonic() - at < ttl:
        return url

    url = ""
    try:
        from langsmith import Client

        client = Client(timeout_ms=2000)
        project = client.read_project(project_name=settings.langsmith_project)
        # 与官方 get_run_url 同款格式：/o/<tenant>/projects/p/<project-id>
        url = f"{client._host_url}/o/{client._get_tenant_id()}/projects/p/{project.id}"
    except Exception as exc:                      # noqa: BLE001 - 回链失败不影响主流程
        logger.warning("langsmith project url unavailable: %r", exc)
    _URL_CACHE = (url, time.monotonic())
    return url


_URL_CACHE: tuple[str, float] = ("", 0.0)


def public_config() -> dict:
    """给前端的回链信息：只暴露开关、项目名、项目页地址，**不含任何凭据**。

    地址优先取 `LANGSMITH_PROJECT_URL`；没填且追踪开着时自动解析项目页
    （形如 `https://smith.langchain.com/o/<org>/projects/p/<project-id>`）。
    拿不到就是空串，前端按“已开启但未配置链接”处理，不显示死链。
    """
    return {
        "enabled": tracing_enabled(),
        "project": settings.langsmith_project,
        "url": _resolve_project_url(),
    }


def configure_langsmith() -> None:
    """把 settings 落到环境变量——langsmith 只认环境变量，不认我们的 dataclass。"""
    enabled = tracing_enabled()
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if enabled:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    else:
        # 开关关着就把 key 摘掉，避免 SDK 在残留 key 下仍然建客户端、打网络请求
        os.environ.pop("LANGSMITH_API_KEY", None)


def traceable(name: str) -> Callable:
    """给一个函数加 LangSmith span；关闭时返回原函数（不是包装器）。"""

    def decorator(func: Callable) -> Callable:
        if not tracing_enabled():
            return func
        from langsmith import traceable as ls_traceable

        # 显式 run_type：方法默认会被标成 chain，这里保持 chain 但给出可读名字
        wrapped = ls_traceable(run_type="chain", name=name)(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return wrapped(*args, **kwargs)

        return wrapper

    return decorator


# 模块级执行：只要有任何代码 import 到本模块（llm_service / routes / graph 都会），
# 环境变量在 LangGraph 编译图之前就已经就位。放在文件末尾，settings 已就绪。
configure_langsmith()
