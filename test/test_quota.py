# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""quota.py 单测：默认走 DeepSeek、DeepSeek 时显式关思考模式、其余网关不带该字段。"""

import json

import httpx
import pytest

from src.app.llm import quota


def _fake_post(result_json: dict, status: int = 200, headers: dict | None = None):
    """返回一个替身 httpx.post：捕获请求体、回 200 + 给定 JSON。"""
    captured: dict = {}

    def _post(url, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        resp = httpx.Response(
            status, json=result_json,
            headers=headers or {"x-ratelimit-remaining-requests": "149"},
        )
        return resp

    return _post, captured


def test_default_base_is_deepseek_when_url_unset(monkeypatch):
    """LLM_BASE_URL 未配时，ping 也必须打 DeepSeek（默认模型 deepseek-flash）。"""
    monkeypatch.setattr(quota, "LLM_BASE_URL", "")
    monkeypatch.setattr(quota, "LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr(quota, "LLM_API_KEY", "sk-test")

    post, cap = _fake_post({"choices": [{"message": {"content": "p"}}]})
    monkeypatch.setattr(quota.httpx, "post", post)

    out = quota.check_quota()
    assert out["ok"] is True
    assert cap["url"] == "https://api.deepseek.com/chat/completions"
    assert cap["json"]["model"] == "deepseek-flash"


def test_explicit_base_url_wins(monkeypatch):
    """显式配了 LLM_BASE_URL 时，以它为准（不回退 DeepSeek）。"""
    monkeypatch.setattr(quota, "LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(quota, "LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(quota, "LLM_API_KEY", "sk-test")

    post, cap = _fake_post({"choices": [{"message": {"content": "p"}}]})
    monkeypatch.setattr(quota.httpx, "post", post)

    quota.check_quota()
    assert cap["url"] == "https://api.openai.com/v1/chat/completions"


def test_deepseek_ping_disables_thinking(monkeypatch):
    """DeepSeek（按 base_url 或模型名识别）ping 必须带 thinking:disabled。"""
    monkeypatch.setattr(quota, "LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(quota, "LLM_MODEL", "deepseek-chat")
    monkeypatch.setattr(quota, "LLM_API_KEY", "sk-test")

    post, cap = _fake_post({"choices": [{"message": {"content": "p"}}]})
    monkeypatch.setattr(quota.httpx, "post", post)

    quota.check_quota()
    assert cap["json"].get("thinking") == {"type": "disabled"}
    assert cap["json"]["max_tokens"] == 1


def test_non_deepseek_ping_has_no_thinking_field(monkeypatch):
    """非 DeepSeek 网关（如 OpenAI）不附加 thinking 字段，避免未知字段被拒。"""
    monkeypatch.setattr(quota, "LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(quota, "LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(quota, "LLM_API_KEY", "sk-test")

    post, cap = _fake_post({"choices": [{"message": {"content": "p"}}]})
    monkeypatch.setattr(quota.httpx, "post", post)

    quota.check_quota()
    assert "thinking" not in cap["json"]


def test_missing_api_key_returns_ok_false(monkeypatch):
    monkeypatch.setattr(quota, "LLM_API_KEY", "")
    out = quota.check_quota()
    assert out == {"ok": False, "reason": "LLM_API_KEY 未配置，无法查询额度"}


def test_429_returns_limited_with_headers(monkeypatch):
    """429 时 ok=True、limited=True，且把 retry-after 等头部回传。"""
    monkeypatch.setattr(quota, "LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(quota, "LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr(quota, "LLM_API_KEY", "sk-test")

    post, _ = _fake_post(
        {"error": {"message": "rate limited"}},
        status=429,
        headers={"retry-after": "30", "x-ratelimit-remaining-requests": "0"},
    )
    monkeypatch.setattr(quota.httpx, "post", post)

    out = quota.check_quota()
    assert out["ok"] is True and out["limited"] is True
    assert out["quota"].get("retry-after") == "30"
