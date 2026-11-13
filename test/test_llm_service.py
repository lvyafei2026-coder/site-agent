# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""llm_service._complete 的兼容性与降级：覆盖线上「LLM_BASE_URL 返回裸字符串」场景。

此前写死 resp.choices[0].message.content，遇到返回裸字符串的服务就抛
'str' object has no attribute 'choices'，整轮抽取被降级成正则兜底。这里验证：
- 裸字符串 / 标准对象 / dict / 顶层 content / 字符串 choices 都能归一；
- JSON 外层的 ```json 围栏能被剥掉；
- 返回形态异常不再被误判成「故障」（failed 不应被置 True）。
"""

from types import SimpleNamespace

import pytest

import src.app.services.llm_service as llm_mod
from src.app.services.llm_service import LLMService


def _msg(content):
    return SimpleNamespace(message=SimpleNamespace(content=content))


def _obj_resp(content):
    return SimpleNamespace(choices=[_msg(content)])


# ---- _content_from_response 四种形态 ----

def test_content_from_str():
    assert LLMService._content_from_response('{"a":1}') == '{"a":1}'


def test_content_from_obj():
    assert LLMService._content_from_response(_obj_resp('{"a":2}')) == '{"a":2}'


def test_content_from_dict():
    assert LLMService._content_from_response(
        {"choices": [{"message": {"content": '{"a":3}'}}]}) == '{"a":3}'


def test_content_from_top_level_dict():
    assert LLMService._content_from_response({"content": '{"a":4}'}) == '{"a":4}'


def test_content_from_string_choice():
    assert LLMService._content_from_response(
        SimpleNamespace(choices=['{"a":5}'])) == '{"a":5}'


def test_content_from_none():
    assert LLMService._content_from_response(None) is None


# ---- _strip_fences ----

def test_strip_fenced_json():
    assert LLMService._strip_fences('```json\n{"a":1}\n```') == '{"a":1}'


def test_strip_plain_json():
    assert LLMService._strip_fences('{"a":1}') == '{"a":1}'


# ---- _complete 端到端：模拟 get_llm 返回裸字符串 ----

class _FakeCompletions:
    def __init__(self, payload):
        self._payload = payload

    def create(self, **_kwargs):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.chat = SimpleNamespace(completions=_FakeCompletions(payload))


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(llm_mod, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(llm_mod, "LLM_MODEL", "test-model")


def test_complete_accepts_bare_string(with_key, monkeypatch):
    """线上 LLM_BASE_URL 直接返回裸 JSON 字符串：必须能解析、且不误判故障。"""
    monkeypatch.setattr(llm_mod, "get_llm", lambda: _FakeClient('{"intent": "general", "confidence": 0.9}'))

    data, failed, retryable = llm_mod.llm_service._complete("sys", "user", "warn_event")
    assert data == {"intent": "general", "confidence": 0.9}
    assert failed is False
    assert retryable is False


def test_complete_accepts_fenced_string(with_key, monkeypatch):
    monkeypatch.setattr(
        llm_mod, "get_llm",
        lambda: _FakeClient('```json\n{"intent": "general"}\n```'))

    data, failed, retryable = llm_mod.llm_service._complete("sys", "user", "warn_event")
    assert data == {"intent": "general"}
    assert failed is False


def test_complete_accepts_standard_object(with_key, monkeypatch):
    monkeypatch.setattr(llm_mod, "get_llm", lambda: _FakeClient(_obj_resp('{"intent": "plan_promo"}')))

    data, failed, _ = llm_mod.llm_service._complete("sys", "user", "warn_event")
    assert data == {"intent": "plan_promo"}


def test_complete_empty_content_not_failed(with_key, monkeypatch):
    """content 取不到：按「未解析到结构化数据」降级（failed=False），不短路。"""
    monkeypatch.setattr(llm_mod, "get_llm", lambda: _FakeClient(object()))

    data, failed, retryable = llm_mod.llm_service._complete("sys", "user", "warn_event")
    assert data is None
    assert failed is False
    assert retryable is False


def test_complete_network_error_is_retryable(with_key, monkeypatch):
    def boom(**_kwargs):
        raise TimeoutError("connection timed out")

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    monkeypatch.setattr(llm_mod, "get_llm", lambda: fake)

    data, failed, retryable = llm_mod.llm_service._complete("sys", "user", "warn_event")
    assert data is None
    assert failed is True
    assert retryable is True
