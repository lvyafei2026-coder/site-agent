# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 调用：意图分类 + 字段抽取。

无 LLM_API_KEY 时走确定性本地回退，图与测试不依赖任何外部凭据或网络。
"""

from __future__ import annotations

import json
import re

from langchain_core.prompts import ChatPromptTemplate

from src.app.config import INTENTS, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_THINKING
from src.app.llm.provider import content_from_response, get_llm
from src.app.observability.langsmith import traceable, tracing_enabled
from src.app.observability.metrics import metrics
from src.app.tools.base import is_retryable_error
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CONFIDENCE = 0.55


def _thinking_kwargs() -> dict:
    """DeepSeek 思考模式默认开启，分析类文案任务需要关掉（与 hotwords.py 的 _thinking_kwargs 一致）。

    auto（默认）：仅当 LLM_BASE_URL 指向 DeepSeek 时注入 extra_body；换用别的
      OpenAI 兼容服务不会误传这个 DeepSeek 专属参数；
    off：强制注入；on：保留思考（此时要自行调大 max_tokens）。
    """
    mode = (LLM_THINKING or "auto").strip().lower()
    if mode == "on":
        return {}
    if mode == "off" or (mode == "auto" and "deepseek" in (LLM_BASE_URL or "").lower()):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


class LLMService:
    def __init__(self) -> None:
        # extract_fields 调用后写入：failed=True 表示「已配置 key 但本次调用抛异常」；
        # retryable=True 表示该异常是网络/连接层瞬态故障（应走重试），False 表示明确
        # 非网络失败（鉴权/配额/参数）。processing_node 据此决定：明确失败 → 短路到
        # ERROR 等下次请求；网络超时 → 不短路，交 validation 走重试逻辑。
        self.extract_failed: bool = False
        self.extract_retryable: bool = False

    @traceable(name="classify_intent")
    def classify_intent(self, template: ChatPromptTemplate, text: str) -> dict:
        """把请求归入 INTENTS 之一；无 key 或调用失败时回退 general。"""
        intent, confidence = "general", DEFAULT_CONFIDENCE
        raw, _, _ = self._complete(template.format(input_text=text), text, "intent_llm_failed") or (None, False, False)
        data = raw or {}
        if data:
            intent = str(data.get("intent", intent)).strip().lower()
            try:
                confidence = float(data.get("confidence", confidence))
            except (TypeError, ValueError):
                pass
        if intent not in INTENTS:
            intent = "general"
        metrics.increment("llm.classify_intent.calls")
        return {"intent": intent, "confidence": confidence}

    @traceable(name="extract_fields")
    def extract_fields(self, template: ChatPromptTemplate, text: str, intent: str) -> dict:
        """按 ExtractionSchema 的字段抽事实；LLM 不可用时用正则兜底，保证字段齐全。"""
        raw, failed, retryable = self._complete(
            template.format(input_text=text, intent=intent), text, "extract_llm_failed") or (None, False, False)
        self.extract_failed = failed
        self.extract_retryable = retryable
        data = raw or {}
        metrics.increment("llm.extract_fields.calls")
        return {
            "target_site": str(data.get("target_site", "")),
            "intent": intent,
            "keywords": list(data.get("keywords")
                             or re.findall(r"[A-Za-z][A-Za-z0-9 -]{2,30}", text))[:12],
            "pages": list(data.get("pages") or []),
            "notes": str(data.get("notes") or text[:200]),
            "requires_tool": bool(data.get("requires_tool", True)),
        }

    @traceable(name="analyze_metrics")
    def analyze_metrics(self, system_prompt: str, data: str) -> str:
        """对采集到的站点埋点 / GSC / 页面数据做自然语言分析，识别表现好的站点。

        返回 markdown 文本（结论 + 表现好的站点排名 + 理由）；无 key 或调用失败返回
        空串（上层走确定性回退，不阻塞整轮）。提示词来自 prompts/versions/review_metrics，
        不在代码里硬编码（test/test_prompts.py 守着）。
        """
        if not LLM_API_KEY:
            return ""
        kwargs: dict = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": data},
            ],
            "temperature": 0.2,
        }
        kwargs.update(_thinking_kwargs())
        try:
            resp = get_llm().chat.completions.create(**kwargs)
            return content_from_response(resp) or ""
        except Exception as exc:  # noqa: BLE001 - 分析失败不影响整轮
            logger.warning("analyze_metrics_failed", extra={"error": str(exc)[:200]})
            return ""

    @staticmethod
    def _strip_fences(text: str) -> str:
        """剥掉模型偶尔包在 JSON 外的 ```json ... ``` 围栏。"""
        s = text.strip()
        if s.startswith("```"):
            s = s.split("```", 2)[1]
            if s.lstrip().startswith("json"):
                s = s.lstrip()[4:]
            s = s.strip()
        return s

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        """把模型返回的文本解析成 dict；容忍两种情况：
        1) 被 ```json 围栏包裹；2) 散文里夹着一段 JSON（抽第一个 {…} 对象）。

        OpenAI 兼容服务偶尔忽略 response_format，返回散文/带说明文字而非纯 JSON；
        这里尽量「捞」出 JSON 对象，捞不到返回 None（上层走确定性回退）。
        """
        s = LLMService._strip_fences(text or "").strip()
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

    @staticmethod
    def _cap(s: str | None, n: int = 2000) -> str:
        if s is None:
            return ""
        s = s if isinstance(s, str) else str(s)
        return s if len(s) <= n else s[:n] + f"...<truncated {len(s) - n} chars>"

    @staticmethod
    def _complete(prompt: str, text: str, warn_event: str) -> tuple[dict | None, bool, bool]:
        """调 OpenAI 兼容接口并解析 JSON；任何异常都降级，不阻塞整轮。

        返回 (data, failed, retryable)：
        - 无 key          → (None, False, False)：预期关闭（确定性回退），不算故障；
        - 成功解析到 dict → (data, False, False)；
        - 调通了但内容非 JSON（散文/错误页）→ (None, False, False)：按「未解析到结构化
          数据」降级回退，不置 failed——这是内容形态问题（重试也救不回），且我们有正则
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
            "system_prompt": LLMService._cap(prompt, 2000),
            "user_text": LLMService._cap(text, 2000),
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
                "content_head": LLMService._cap(content, 400),
            })
            if content is None:
                logger.debug("llm_empty_content", extra={"warn_event": warn_event})
                return None, False, False
            after_strip = LLMService._strip_fences(content)
            logger.info("llm_after_strip_fences", extra={"text_head": LLMService._cap(after_strip, 400)})
            data = LLMService._parse_json(content)
            logger.info("llm_parse_result", extra={
                "ok": data is not None,
                "keys": list(data.keys()) if data else None,
                "head": LLMService._cap(content, 200),
            })
            if data is None:
                logger.warning("llm_nonjson_response",
                               extra={"warn_event": warn_event, "head": LLMService._cap(content, 400)})
                return None, False, False
            return data, False, False
        except Exception as exc:  # noqa: BLE001 - LLM 不可用不影响整轮
            logger.warning(warn_event, extra={"error": str(exc)[:200]})
            return None, True, is_retryable_error(exc)


llm_service = LLMService()
