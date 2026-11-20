# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 补全底座：JSON 结构化补全 + 纯文本补全，含解析与降级。

只管「怎么问模型、怎么把回答读出来、读不出来怎么降级」，不含任何业务语义——
意图识别 / 字段抽取 / 流量分析各自在 services 的对应实现类里。

无 LLM_API_KEY 时走确定性回退，图与测试不依赖任何外部凭据或网络。
"""

from __future__ import annotations

import json
import re

from src.app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_THINKING
from src.app.enums import LlmThinking
from src.app.llm.provider import content_from_response, get_llm
from src.app.observability.langsmith import tracing_enabled
# base.py 是纯叶子（只依赖标准库 / pydantic / enums），这里取异常分类不会引回环
from src.app.tools.base import is_retryable_error
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def thinking_kwargs() -> dict:
    """DeepSeek 思考模式默认开启，分析类文案任务需要关掉。

    auto（默认）：仅当 LLM_BASE_URL 指向 DeepSeek 时注入 extra_body；换用别的
      OpenAI 兼容服务不会误传这个 DeepSeek 专属参数；
    off：强制注入；on：保留思考（此时要自行调大 max_tokens）。
    """
    mode = (LLM_THINKING or LlmThinking.AUTO.value).strip().lower()
    if mode == LlmThinking.ON.value:
        return {}
    if mode == LlmThinking.OFF.value or (
            mode == LlmThinking.AUTO.value and "deepseek" in (LLM_BASE_URL or "").lower()):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def strip_fences(text: str) -> str:
    """剥掉模型偶尔包在 JSON 外的 ```json ... ``` 围栏。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
        s = s.strip()
    return s


def parse_json(text: str) -> dict | None:
    """把模型返回的文本解析成 dict；容忍两种情况：
    1) 被 ```json 围栏包裹；2) 散文里夹着一段 JSON（抽第一个 {…} 对象）。

    OpenAI 兼容服务偶尔忽略 response_format，返回散文/带说明文字而非纯 JSON；
    这里尽量「捞」出 JSON 对象，捞不到返回 None（上层走确定性回退）。
    """
    s = strip_fences(text or "").strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        mobj = re.search(r"\{.*\}", s, re.DOTALL)
        if not mobj:
            return None
        try:
            data = json.loads(mobj.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _cap(s: str | None, n: int = 2000) -> str:
    if s is None:
        return ""
    s = s if isinstance(s, str) else str(s)
    return s if len(s) <= n else s[:n] + f"...<truncated {len(s) - n} chars>"


def complete_json(prompt: str, text: str, warn_event: str) -> tuple[dict | None, bool, bool]:
    """调 OpenAI 兼容接口并解析 JSON；任何异常都降级，不阻塞整轮。

    返回 (data, failed, retryable)：
    - 无 key          → (None, False, False)：预期关闭（确定性回退），不算故障；
    - 成功解析到 dict → (data, False, False)；
    - 调通了但内容非 JSON（散文/错误页）→ (None, False, False)：按「未解析到结构化
      数据」降级回退，不置 failed——这是内容形态问题（重试也救不回），且上层有正则
      兜底，不该触发短路到 ERROR 把整轮作废；
    - 异常且为网络层  → (None, True,  True )：连接/超时类，应走重试；
    - 异常且明确失败  → (None, True,  False)：鉴权/配额/参数，短路到 ERROR 等下次请求。

    诊断：每次调用都会打印 (1) 完整请求（解析出的真实 URL、model、prompt、response_format、
    tracing 开关）；(2) 原始响应对象的形态与内容；(3) 每个解析阶段（取 content / 去围栏 /
    解析 JSON）的中间结果。再配合 LLM_DEBUG=1 的底层 httpx 请求/响应钩子，可一次性定位
    非 JSON 响应究竟是网关拦截、代理改写还是模型真回了异常内容。
    """
    if not LLM_API_KEY:
        return None, False, False
    resolved_url = (f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
                    if LLM_BASE_URL else "https://api.openai.com/v1/chat/completions")
    logger.info("llm_request", extra={
        "model": LLM_MODEL,
        "base_url": LLM_BASE_URL,
        "resolved_url": resolved_url,
        "traced": tracing_enabled(),
        "response_format": "json_object",
        "system_prompt": _cap(prompt, 2000),
        "user_text": _cap(text, 2000),
    })
    try:
        resp = get_llm().chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": prompt},
                      {"role": "user", "content": text}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        # 原始响应对象的形态与内容：直接判断 resp 到底是 ChatCompletion / dict / 裸字符串
        try:
            if hasattr(resp, "model_dump"):
                rd = resp.model_dump()
                choices = rd.get("choices") or []
                raw_info: dict = {
                    "resp_type": type(resp).__name__,
                    "id": rd.get("id"),
                    "model": rd.get("model"),
                    "object": rd.get("object"),
                    "choices": len(choices),
                    "first_role": (choices[0].get("message", {}).get("role")
                                   if choices else None),
                    "first_content_head": ((choices[0].get("message", {}).get("content") or "")[:400]
                                           if choices else None),
                    "usage": rd.get("usage"),
                }
            else:
                raw_info = {"resp_type": type(resp).__name__,
                            "repr": repr(resp)[:500]}
        except Exception as dump_err:  # noqa: BLE001
            raw_info = {"resp_type": type(resp).__name__,
                        "dump_error": str(dump_err)[:200]}
        logger.info("llm_raw_response", extra=raw_info)

        content = content_from_response(resp)
        logger.info("llm_content_extracted", extra={
            "content_type": type(content).__name__,
            "content_head": _cap(content, 400),
        })
        if content is None:
            logger.debug("llm_empty_content", extra={"warn_event": warn_event})
            return None, False, False
        after_strip = strip_fences(content)
        logger.info("llm_after_strip_fences", extra={"text_head": _cap(after_strip, 400)})
        data = parse_json(content)
        logger.info("llm_parse_result", extra={
            "ok": data is not None,
            "keys": list(data.keys()) if data else None,
            "head": _cap(content, 200),
        })
        if data is None:
            logger.warning("llm_nonjson_response",
                           extra={"warn_event": warn_event, "head": _cap(content, 400)})
            return None, False, False
        return data, False, False
    except Exception as exc:  # noqa: BLE001 - LLM 不可用不影响整轮
        logger.warning(warn_event, extra={"error": str(exc)[:200]})
        return None, True, is_retryable_error(exc)


def complete_text(system_prompt: str, text: str, *, temperature: float = 0.2,
                  warn_event: str = "llm_text_failed") -> str:
    """纯文本补全（分析类文案）：返回 markdown 正文，无 key 或调用失败返回空串。

    与 complete_json 的区别：不要求 response_format、不解析 JSON，失败只降级成
    空串由上层跳过该段，绝不阻塞整轮。
    """
    if not LLM_API_KEY:
        return ""
    kwargs: dict = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": temperature,
    }
    kwargs.update(thinking_kwargs())
    try:
        resp = get_llm().chat.completions.create(**kwargs)
        return content_from_response(resp) or ""
    except Exception as exc:  # noqa: BLE001 - 分析失败不影响整轮
        logger.warning(warn_event, extra={"error": str(exc)[:200]})
        return ""