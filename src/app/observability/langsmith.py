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
from collections.abc import Callable

from src.app.config import settings
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def tracing_enabled() -> bool:
    """真开追踪的条件：开关为真 + 有 key。"""
    return bool(settings.langsmith_tracing and settings.langsmith_api_key)


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
