# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""quota.py 单测：官方余额零消耗查询、平台用量聚合、登录态失效与非 DeepSeek 网关降级。"""

from __future__ import annotations

import httpx

from src.app.llm import quota

_BALANCE = {
    "is_available": True,
    "balance_infos": [{
        "currency": "CNY", "total_balance": "8.09",
        "granted_balance": "0.00", "topped_up_balance": "8.09",
    }],
}


def _fake_get(routes: dict):
    """按 URL 片段分派假响应，并记录每次调用（url / headers / params）。"""
    calls: list[dict] = []

    def _get(url, *, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": headers, "params": params,
                      "timeout": timeout})
        for fragment, payload in routes.items():
            if fragment in url:
                return httpx.Response(payload.get("status", 200), json=payload["json"])
        return httpx.Response(404, json={"error": "not mocked: " + url})

    return _get, calls


def _setup(monkeypatch, *, token: str = "") -> None:
    monkeypatch.setattr(quota, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(quota, "LLM_BASE_URL", "")
    monkeypatch.setattr(quota, "LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr(quota, "DEEPSEEK_PLATFORM_TOKEN", token)


def test_missing_api_key_returns_ok_false(monkeypatch):
    monkeypatch.setattr(quota, "LLM_API_KEY", "")
    assert quota.check_quota() == {
        "ok": False, "reason": "LLM_API_KEY 未配置，无法查询额度"}


def test_balance_uses_official_endpoint_without_spending_quota(monkeypatch):
    """只打 /user/balance：不再发 ping 推理请求，不消耗请求额度。"""
    _setup(monkeypatch)
    get, calls = _fake_get({"/user/balance": {"json": _BALANCE}})
    monkeypatch.setattr(quota.httpx, "get", get)

    out = quota.check_quota()
    assert out["ok"] is True
    assert len(calls) == 1                      # 没有 /chat/completions ping
    assert calls[0]["url"] == "https://api.deepseek.com/user/balance"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert out["balance"] == {
        "ok": True, "available": True, "currency": "CNY",
        "total": "8.09", "topped_up": "8.09", "granted": "0.00",
    }
    assert out["usage"]["ok"] is False          # 未配登录态 token 时明确降级
    assert "DEEPSEEK_PLATFORM_TOKEN" in out["usage"]["reason"]
    assert out["checked_at_bj"]


def test_usage_parses_amount_cost_and_summary(monkeypatch):
    """近 30 天消费 / 请求次数 / Tokens / 累计消费，窗口按北京时间 UTC+8。"""
    _setup(monkeypatch, token="ut-test")
    routes = {
        "/user/balance": {"json": _BALANCE},
        "by_api_key/amount": {"json": {"code": 0, "biz_data": {
            "series": [{"api_key": "k1", "model": "deepseek-flash", "buckets": [
                {"time": 1, "usage": {"PROMPT_CACHE_HIT_TOKEN": 100,
                                       "PROMPT_CACHE_MISS_TOKEN": 80,
                                       "RESPONSE_TOKEN": 20, "REQUEST": 2}},
                {"time": 2, "usage": {"RESPONSE_TOKEN": 0, "REQUEST": 70}},
            ]}]}}},
        "by_api_key/cost": {"json": {"code": 0, "biz_data": {
            "data": [{"currency": "CNY", "series": [
                {"api_key": "k1", "model": "deepseek-flash",
                 "buckets": [{"time": 1, "cost": 0.32}]}]}]}}},
        "get_user_summary": {"json": {"code": 0, "biz_data": {
            "total_costs": [{"currency": "CNY", "amount": 296.90}]}}},
    }
    get, calls = _fake_get(routes)
    monkeypatch.setattr(quota.httpx, "get", get)

    out = quota.check_quota()
    usage = out["usage"]
    assert usage["ok"] is True
    assert usage["tokens"] == 200
    assert usage["requests"] == 72
    assert usage["cost"] == 0.32
    assert usage["total_cost"] == 296.9
    assert usage["currency"] == "CNY"
    assert usage["window_days"] == 30

    amount_call = next(c for c in calls if "by_api_key/amount" in c["url"])
    assert amount_call["params"]["tz"] == 8 * 3600
    assert amount_call["params"]["bucket"] == 86400
    assert amount_call["params"]["end"] - amount_call["params"]["start"] == 30 * 86400
    assert amount_call["headers"]["Authorization"] == "Bearer ut-test"


def test_platform_login_expired_degrades_without_hiding_balance(monkeypatch):
    """code 40003（登录态失效）只让 usage 失败，余额照常可用。"""
    _setup(monkeypatch, token="expired")
    bad = {"code": 40003, "msg": "Authorization Failed (invalid token)"}
    get, _ = _fake_get({
        "/user/balance": {"json": _BALANCE},
        "by_api_key/amount": {"json": bad},
        "by_api_key/cost": {"json": bad},
        "get_user_summary": {"json": bad},
    })
    monkeypatch.setattr(quota.httpx, "get", get)

    out = quota.check_quota()
    assert out["ok"] is True
    assert out["balance"]["ok"] is True
    assert out["usage"]["ok"] is False
    assert "40003" in out["usage"]["reason"]
    assert "登录态" in out["usage"]["reason"]


def test_non_deepseek_gateway_skips_balance(monkeypatch):
    """其它网关没有 /user/balance，明确报错而不是猜路径。"""
    _setup(monkeypatch)
    monkeypatch.setattr(quota, "LLM_BASE_URL", "https://api.openai.com/v1")
    get, calls = _fake_get({})
    monkeypatch.setattr(quota.httpx, "get", get)

    out = quota.check_quota()
    assert out["ok"] is False
    assert "不支持官方余额接口" in out["reason"]
    assert calls == []


def test_amount_field_rename_falls_back_to_numeric_sum(monkeypatch):
    """面板 token 字段改名时不静默归零：除 REQUEST 外的数值仍累加。"""
    _setup(monkeypatch, token="ut-test")
    get, _ = _fake_get({
        "/user/balance": {"json": _BALANCE},
        "by_api_key/amount": {"json": {"biz_data": {"series": [{
            "buckets": [{"usage": {"INPUT_TOKEN": "60",
                                    "OUTPUT_TOKEN": 40, "REQUEST": 5}}]}]}}},
        "by_api_key/cost": {"json": {"biz_data": {"data": [
            {"currency": "CNY", "series": [{"buckets": [{"cost": "0.10"}]}]}]}}},
        "get_user_summary": {"json": {"biz_data": {}}},
    })
    monkeypatch.setattr(quota.httpx, "get", get)

    usage = quota.check_quota()["usage"]
    assert usage["ok"] is True
    assert usage["tokens"] == 100
    assert usage["requests"] == 5
    assert usage["cost"] == 0.1
    assert "total_cost" not in usage
