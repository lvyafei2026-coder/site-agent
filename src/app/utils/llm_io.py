# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 传输层：非流式补全调用、思考开关、空正文现场诊断（无任何业务语义）。

只管「把 messages 发出去、把正文拿回来」，不解析内容——解析在各自业务的判据层
（热词线在 services/hotword_criteria.py，messages 由 prompts/hotword_messages.py 拼好传入）。

与同层其它模块的分工：
    src/app/llm/provider.py     建连接与客户端复用（get_llm / content_from_response）
    src/app/llm/completion.py   JSON / 纯文本补全与降级，复用本模块的 thinking_kwargs
    本模块                      一次调用的参数拼装 + 「正文为空」到底为什么

★DeepSeek 思考模式默认开启（effort=high），reasoning_content 单独返回且计入
  max_tokens：实测 2000 全被推理吃光 → finish_reason=length、content 为空。
  所以固定格式的文案任务会关掉思考（见 thinking_kwargs），把预算全给正文；
  若手动设 LLM_THINKING=on 保留思考，需自行把调用方传的 max_tokens 调大。
"""

from __future__ import annotations

import logging
from typing import Any

from src.app import config
from src.app.enums import LlmThinking
from src.app.llm import provider

log = logging.getLogger(__name__)


class LLMEmptyContent(Exception):
    """LLM 调用已完成但没有可用正文。message 是准确的、可操作的失败说明。

    与「LLM 调用失败」（连接/鉴权异常）分开：这类是这一次没产出内容，不代表整批
    模型不可用，调用方不应据此中止整批。
    """

    def __init__(self, message: str, diag: dict[str, Any]) -> None:
        super().__init__(message)
        self.diag = diag


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """响应可能是标准对象、wrap_openai 的 dict、或 None，统一取值。"""
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def thinking_kwargs() -> dict[str, Any]:
    """DeepSeek 思考模式默认开启，分析类文案任务需要关掉。

    auto（默认）：仅当 LLM_BASE_URL 指向 DeepSeek 时注入 extra_body，换用别的
      OpenAI 兼容服务不会误传这个 DeepSeek 专属参数；
    off：强制注入；on：保留思考（此时要自行调大 max_tokens）。
    """
    mode = (config.LLM_THINKING or LlmThinking.AUTO.value).strip().lower()
    if mode == LlmThinking.ON.value:
        return {}
    if mode == LlmThinking.OFF.value or (
            mode == LlmThinking.AUTO.value and "deepseek" in (config.LLM_BASE_URL or "").lower()):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def empty_content_diag(resp: Any, max_tokens: int) -> dict[str, Any]:
    """把「正文为空」的现场读全，并给出与真实原因一致的错误说明。

    实测线上场景：finish_reason=length、content=""、reasoning_tokens=2000、
    completion_tokens=2000 —— 是输出预算被思考推理吃光，绝不是 API Key 没配。
    """
    choices = _field(resp, "choices") or []
    first = choices[0] if choices else None
    finish = _field(first, "finish_reason")
    usage = _field(resp, "usage") or {}
    details = _field(usage, "completion_tokens_details") or {}
    reasoning = _field(details, "reasoning_tokens")
    if reasoning is None:                       # 兼容扁平 usage / LangSmith 形态
        reasoning = _field(usage, "reasoning_tokens")
    if reasoning is None:
        out_details = _field(_field(resp, "usage_metadata") or {},
                             "output_token_details") or {}
        reasoning = _field(out_details, "reasoning")
    completion = _field(usage, "completion_tokens")
    model = _field(resp, "model") or config.LLM_MODEL

    if finish == "length" and reasoning:
        reason = "输出预算耗尽（思考推理吃光 max_tokens，正文未生成）"
        hint = "关闭思考模式（LLM_THINKING=off/auto）或调大 max_tokens"
    elif finish == "length":
        # 已经没在思考却还是 length，说明是正文自己超了预算，不能赖到推理头上
        reason = "输出预算耗尽（正文超出 MAX_TOKENS，本次未产生推理 token）"
        hint = f"调大 MAX_TOKENS（当前 {max_tokens}）"
    elif not choices:
        reason = "响应中没有 choices"
        hint = "检查 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 与响应形态"
    elif _field(first, "message") is None:
        reason = "响应形态异常（choice 缺 message）"
        hint = "检查 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 与响应形态"
    else:
        reason = "模型返回空 content"
        hint = "检查 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 与响应形态"

    diag = {
        "reason": reason, "hint": hint, "model": model,
        "finish_reason": finish, "reasoning_tokens": reasoning,
        "completion_tokens": completion, "max_tokens": max_tokens,
        "resp_type": type(resp).__name__, "choices": len(choices),
    }
    diag["message"] = (
        f"LLM 返回空正文：{reason}；finish_reason={finish}, "
        f"max_tokens={max_tokens}, reasoning_tokens={reasoning}, "
        f"completion_tokens={completion}, model={model}, "
        f"resp_type={type(resp).__name__}, choices={len(choices)}；建议：{hint}"
    )
    return diag


def complete(messages: list[dict[str, str]], *, max_tokens: int) -> tuple[str, str]:
    """非流式调用，返回 (正文, 模型名)；正文为空抛 LLMEmptyContent（带准确诊断）。

    max_tokens 由调用方按自己的输出量给——它是业务口径（正文要写多长），不属于传输层。

    ★走统一的 get_llm()：与其它调用方共用同一客户端（含 LangSmith trace 与
      LLM_DEBUG 的 HTTP 钩子），调用不再是 trace/日志的盲区。响应经
      content_from_response 归一——wrap_openai 会把 ChatCompletion 转成 dict，
      裸写 resp.choices[0].message.content 在这条路径上会直接抛 AttributeError。
    """
    if not config.LLM_API_KEY:
        diag = {"reason": "LLM_API_KEY 未配置，未发起调用", "no_key": True,
                "hint": "在 Render 配好 LLM_API_KEY", "finish_reason": None,
                "reasoning_tokens": None, "max_tokens": max_tokens}
        diag["message"] = "LLM 未配置 API Key（LLM_API_KEY 为空），本次未发起调用；建议在 Render 配好该变量"
        raise LLMEmptyContent(diag["message"], diag)

    resp = provider.get_llm().chat.completions.create(
        model=config.LLM_MODEL, messages=messages,
        max_tokens=max_tokens, temperature=0, timeout=60,
        **thinking_kwargs(),
    )
    text = (provider.content_from_response(resp) or "").strip()
    if not text:
        diag = empty_content_diag(resp, max_tokens)
        log.warning("llm_empty_content %s", diag["message"])
        raise LLMEmptyContent(diag["message"], diag)
    return text, config.LLM_MODEL
