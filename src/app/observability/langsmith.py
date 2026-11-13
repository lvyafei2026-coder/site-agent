# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LangSmith 追踪：唯一开关处。

三条原则：
  1. 开关只认 `settings`（.env / Render 环境变量），业务代码不直接读 LANGSMITH_*。
  2. `LANGSMITH_TRACING=true` **且** 有 `LANGSMITH_API_KEY` 才算开启——只有开关没
     有 key 时发送必然 401，会在日志里刷一堆噪音，不如直接当关闭。
  3. 关闭时零开销：`traceable` 原样返回函数，不 import langsmith 运行时。

开启后会自动产生五层 trace：
  - 图级：LangGraph 每次 `invoke` 一条 run（名字由调用方传 run_name）
  - 节点级：classify_intent / run_processing / validate_result / build_output …
  - 工具级：`tool:<名字>`（本文件 `trace_tool`，run_type=tool）记 26 个 RuntimeTool
    的入参/返回/错误；热词批次内部另有 `tool:serp` / `tool:trend` / `tool:kv.write`
    等 IO span，把 SERP、Trends、KV 读写这些原本看不见的调用也挂到对应词下
  - 批次/逐词级：`run_traced`（本文件）给 run_batch 与每个热词各建一条 span
  - LLM 级：`llm/provider.py` 用 `wrap_openai` 包过的客户端，请求体与返回值都在

★为什么要 `run_traced` 的显式 parent：ThreadPoolExecutor 的 worker 线程**不继承
  主线程的 contextvars**，而 LangSmith 把父 run 存在 contextvar 里。并发逐词分析时
  wrap_openai 找不到父，20 个 ChatOpenAI 全变成独立根 trace（Traces 页面「连不起来」）。
  显式把父 run 传进去即可拼成 graph 根 → 批次 → 每词 → LLM 的一棵树。
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections.abc import Callable
from typing import Any

from src.app.config import settings
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

# LangSmith 的 inputs/outputs 会随整棵 run 一起上报。某个工具返回整张候选表、
# 或 AI 正文这种大字符串时，全量塞进去既拖慢上报也把 UI 卡住，所以统一裁剪：
# 单个字符串最多 4000 字符、单个容器最多 60 项、递归最多 6 层。
MAX_IO_CHARS = 4000
MAX_IO_ITEMS = 60
MAX_IO_DEPTH = 6


def compact_io(value: Any, *, max_chars: int = MAX_IO_CHARS,
               _depth: int = 0) -> Any:
    """把任意工具出入参变成「可序列化、不会撑爆 trace」的纯数据。

    逐项裁剪而不是整体截断：保留结构（哪个字段、哪一条），只把过长的值截断并
    标注原长度，排查时仍能看到字段名与前后文。pydantic / dataclass 等对象降级
    `model_dump()` 或 str()，绝不因为一个不可序列化对象让整条 trace 上报失败。
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return value[:max_chars] + f"…[原长 {len(value)} 字符，已截断]"
    if _depth >= MAX_IO_DEPTH:
        return str(value)[:max_chars]
    if isinstance(value, dict):
        return {str(k)[:120]: compact_io(v, max_chars=max_chars, _depth=_depth + 1)
                for k, v in list(value.items())[:MAX_IO_ITEMS]}
    if isinstance(value, (list, tuple, set, frozenset)):
        seq = list(value)
        out = [compact_io(v, max_chars=max_chars, _depth=_depth + 1)
               for v in seq[:MAX_IO_ITEMS]]
        if len(seq) > MAX_IO_ITEMS:
            out.append(f"…[另有 {len(seq) - MAX_IO_ITEMS} 项]")
        return out
    if hasattr(value, "model_dump"):
        try:
            return compact_io(value.model_dump(), max_chars=max_chars, _depth=_depth + 1)
        except Exception:  # noqa: BLE001 - 降级为字符串即可
            pass
    return str(value)[:max_chars]


def _compact_envelope(value: Any) -> Any:
    """最后一道总大小保护：极端嵌套下逐项裁剪仍可能很大，序列化后再兜一次。"""
    out = compact_io(value)
    try:
        text = json.dumps(out, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return compact_io(str(out))
    if len(text) <= MAX_IO_CHARS * 4:
        return out
    return {"_truncated": True, "size": len(text),
            "preview": text[:MAX_IO_CHARS * 2]}


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


def run_traced(func, *, name: str, inputs: dict | None = None,
               tags: list[str] | None = None, metadata: dict | None = None,
               parent=None, outputs=None, error=None,
               pass_run: bool = False, run_type: str = "chain"):
    """给任意同步函数套一条 LangSmith span —— 薄包，不改 graph、不改业务返回。

    与 `observability/trace.py` 的 `run_traced(graph, state, config)` 不是一回事：那个
    抓的是 LangGraph 节点轨迹（返回给 /run 的 `trace` 字段），这个是给普通函数加 span。

    参数：
      parent: 显式父 run（RunTree / dotted_order 字符串）。**并发场景必须传**——
              worker 线程没有主线程的 contextvar，不传就会新建一条根 trace。
              None 时 SDK 自动取当前线程里的 run（主线程用它挂到 LangGraph 节点下）。
      pass_run: True 时把本次创建的 run 作为唯一位置参数传给 func，批次要拿它当
              逐词 span 的 parent。关闭追踪时传 None。
      outputs / error: 可传 dict / str，也可传 `result -> dict / str` 的回调，
              把结论与业务失败标在 LangSmith 上（error 非空会让该 span 标红）。

    tracing 关闭时零开销：不 import langsmith 运行时，直接返回 func() 的结果。
    """
    if not tracing_enabled():
        return func(None) if pass_run else func()

    from langsmith import trace as ls_trace

    with ls_trace(name, run_type=run_type, inputs=inputs or {},
                  tags=tags or [], metadata=metadata or {}, parent=parent) as run:
        result = func(run) if pass_run else func()
        out = outputs(result) if callable(outputs) else outputs
        err = error(result) if callable(error) else error
        if out is not None or err:
            # SDK 的 __exit__ 只在抛异常时 end；主动 end 才能把 outputs/error 写进
            # 这条 run（官方 trace 示例同款用法），随后 __exit__ 负责 patch 上报。
            run.end(outputs=out or {}, error=err)
        return result


def trace_tool(func, *, name: str, inputs: dict | None = None,
               tags: list[str] | None = None, metadata: dict | None = None,
               parent=None, outputs=None, error=None, run_type: str = "tool"):
    """给一次工具 / 外部 IO 调用建 `run_type=tool` 的 span，出入参全上 LangSmith。

    与 `run_traced` 的区别：那个是业务流程 span（chain），这个是**叶子调用** span，
    名字统一 `tool:<名字>`，Inputs/Outputs 面板里就是这次调用的真实参数与返回摘要。

    ★异常不吞：func 抛异常时 LangSmith 的 `__exit__` 会把异常类型、消息和 traceback
      写进 `error` 并把该 span 标红，异常原样抛给业务方走降级/重试。这正是排查
      「SERP 源不可用」这类降级时最需要的信息——以前它只出现在日志里，Smith 上
      完全看不到 SERP 被调用过。

    tracing 关闭时直接 `func()`：`compact_io`、`run_traced`、langsmith runtime 全部
    不执行，业务返回与未接入追踪前逐字一致。
    """
    if not tracing_enabled():
        return func()

    def _outputs(result):
        raw = outputs(result) if callable(outputs) else outputs
        return _compact_envelope(raw if raw is not None else {})

    def _error(result):
        raw = error(result) if callable(error) else error
        return str(raw)[:1000] if raw else None

    return run_traced(
        func,
        name=name,
        run_type=run_type,
        inputs=_compact_envelope(inputs or {}),
        tags=tags or [],
        metadata=metadata or {},
        parent=parent,
        outputs=_outputs,
        error=_error,
    )


# 模块级执行：只要有任何代码 import 到本模块（llm_service / routes / graph 都会），
# 环境变量在 LangGraph 编译图之前就已经就位。放在文件末尾，settings 已就绪。
configure_langsmith()
