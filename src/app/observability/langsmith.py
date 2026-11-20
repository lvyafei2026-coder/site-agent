# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LangSmith 追踪：唯一开关处 + 声明式切面。

三条原则：
  1. 开关只认 `settings`（.env / Render 环境变量），业务代码不直接读 LANGSMITH_*。
  2. `LANGSMITH_TRACING=true` **且** 有 `LANGSMITH_API_KEY` 才算开启——只有开关没
     有 key 时发送必然 401，会在日志里刷一堆噪音，不如直接当关闭。
  3. 关闭时零开销：`traceable` 原样返回函数；`observed` / `trace_tool` 只多一次
     bool 判断，不 import langsmith 运行时、不算出入参。

切面入口：
  - `observed(...)`  装饰器：业务函数在**定义处**声明 span（名字/出入参/标红/回链），
    函数体零 trace 代码。热词的 run / 取词 / 判例 / 逐词 / serp / trend / kv.write 全走它。
  - `trace_tool(...)` 包裹一次调用：registry 给 25 个 RuntimeTool 统一打点用。
  - `traceable(name)` 装饰器：intent / extraction / metrics 三个实现类的方法用
    （import 期决定是否包装）。

父子关系全靠 LangSmith contextvar 自动继承（链路已改串行），切面**不传 parent**：
    外层 → 热词 run → LangGraph → 节点 → 每词 → IO / LLM
不再需要旧并发模型的显式 parent / pass_run / 手动 thread_id。
底层抓取的诊断用 `note(**fields)` 挂到当前 span，避免层层传 diag dict。
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import time
from collections.abc import Callable
from contextvars import ContextVar
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

# 切面内数据通道：被观察函数在执行途中挂诊断数据（如抓取尝试明细、缓存命中），
# 由 observed 在 span 退出时统一并入 outputs。这让底层 IO 不必再层层传 diag dict。
_span_notes: ContextVar[dict[str, Any] | None] = ContextVar("span_notes", default=None)


def note(**fields: Any) -> None:
    """往当前 span 挂诊断字段；不在切面内或未开追踪时是 no-op（零开销）。"""
    bucket = _span_notes.get()
    if bucket is not None:
        bucket.update(fields)


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


def _call_span(func, *, name: str, run_type: str = "chain", inputs: dict | None = None,
               tags: list[str] | None = None, metadata: dict | None = None,
               outputs=None, error=None):
    """包裹一次调用建 span（trace_tool 的底层）。

    父子靠 contextvar 自动继承，不接 parent / pass_run——那是旧并发模型的残留。
    tracing 关闭时直接 func()，不 import SDK、不算出入参，零开销。
    """
    if not tracing_enabled():
        return func()

    from langsmith import trace as ls_trace

    with ls_trace(name, run_type=run_type,
                  inputs=_compact_envelope(inputs or {}),
                  tags=tags or [], metadata=metadata or {}) as run:
        result = func()
        out = outputs(result) if callable(outputs) else outputs
        err = error(result) if callable(error) else error
        if out is not None or err:
            run.end(outputs=_compact_envelope(out or {}),
                    error=str(err)[:1000] if err else None)
        return result


def trace_tool(func, *, name: str, inputs: dict | None = None,
               tags: list[str] | None = None, metadata: dict | None = None,
               outputs=None, error=None, run_type: str = "tool"):
    """给一次工具 / 外部 IO 调用建 `run_type=tool` 的 span（registry 的统一打点入口）。

    ★异常不吞：func 抛异常时 SDK 把 traceback 写进 span 并标红，异常原样抛给业务方
      走降级/重试。关闭追踪时直接 func()，与未接入追踪前逐字一致。
    """
    return _call_span(func, name=name, run_type=run_type, inputs=inputs,
                      tags=tags, metadata=metadata, outputs=outputs, error=error)


def observed(name, *, kind: str = "chain", inputs=None, outputs=None,
             error=None, tags=None, metadata=None, link: str | None = None):
    """声明式追踪切面：把「建 span + 记出入参 + 标红 + 回链」一次声明在函数上。

    这是「装饰函数」的切面入口；包裹单次调用的场合用 `trace_tool`（registry）。
    业务函数只在**定义处**用装饰器声明观测方式，函数体不出现任何 trace 代码：

        @observed("tool:serp", kind="tool", inputs=serp_inputs, outputs=serp_outputs)
        def _throttled_serp(term, kv): ...

    关键约定：
      - 父子关系靠 LangSmith contextvar 自动继承，切面**不传 parent**。
        串行链路里被观察函数嵌套调用时，子 span 自然挂在调用方的当前 span 下；
        这正是「去掉显式 parent 传递」能成立的前提。
      - inputs / outputs / error 都是纯函数：inputs 收 (*args, **kwargs)，
        outputs / error 收 (result)；关闭追踪时它们一个都不调用（零开销）。
      - link: 若给出字段名，把本次根 run 的回链信息写进返回 dict 的该字段
        （仅用于热词 run_hotwords 这种最外层入口）。
      - 开关在**调用期**判断，不在装饰期：因为测试在运行期才打开追踪。
        关闭时返回的函数体只多一次 `if` 判断，不 import SDK、不建 run。
    """

    def decorator(func: Callable) -> Callable:
        # 为 name / inputs 预先算出「从业务实参里挑哪些传过去」的绑定器：
        # 提取函数只需声明自己关心的参数，不必写 *args 兜底，也不受业务函数签名漂移影响。
        def binder(extractor):
            if extractor is None:
                return None
            sig = inspect.signature(extractor)

            def bind(*args, **kwargs):
                try:
                    bound = sig.bind_partial(*args, **kwargs)
                except TypeError:
                    return None
                return extractor(*bound.args, **bound.kwargs) if bound.arguments else extractor()

            return bind

        name_bind = binder(name) if callable(name) else None
        inputs_bind = binder(inputs)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not tracing_enabled():
                return func(*args, **kwargs)

            from langsmith import trace as ls_trace

            if name_bind:
                span_name = name_bind(*args, **kwargs) or getattr(name, "__name__", "span")
            else:
                span_name = name
            in_data = inputs_bind(*args, **kwargs) if inputs_bind else {}
            notes: dict[str, Any] = {}
            token = _span_notes.set(notes)
            try:
                with ls_trace(span_name, run_type=kind,
                              inputs=_compact_envelope(in_data or {}),
                              tags=tags or [], metadata=metadata or {}) as run:
                    result = func(*args, **kwargs)
                    out = outputs(result) if outputs else None
                    if out is None:
                        out = notes or None
                    elif notes:
                        out = {**out, **notes}
                    err = error(result) if error else None
                    if out is not None or err:
                        run.end(outputs=_compact_envelope(out or {}),
                                error=str(err)[:1000] if err else None)
                    if link and isinstance(result, dict):
                        base = settings.langsmith_project_url.strip().rstrip("/")
                        info: dict[str, Any] = {
                            "traceRunId": str(run.id),
                            "traceId": str(getattr(run, "trace_id", "") or run.id),
                            "url": f"{base}/r/{run.id}?poll=true" if base else "",
                        }
                        # 被观察函数若自带业务 id（如 run_hotwords 的 runId），一并带进回链
                        if result.get("runId"):
                            info["runId"] = result["runId"]
                        result[link] = info
                    return result
            finally:
                _span_notes.reset(token)

        return wrapper

    return decorator


# 模块级执行：只要有任何代码 import 到本模块（llm/completion / routes / graph 都会），
# 环境变量在 LangGraph 编译图之前就已经就位。放在文件末尾，settings 已就绪。
configure_langsmith()
