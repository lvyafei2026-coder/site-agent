# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""quota.py 单测：官方余额零消耗查询、平台双层外壳解析、按 API Key 分组、
登录态失效 / 空数据 / 非 DeepSeek 网关降级。"""

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


def _envelope(biz_data: dict) -> dict:
    """平台真实外壳：{code,msg,data:{biz_code,biz_msg,biz_data}}。"""
    return {"code": 0, "msg": "", "data": {
        "biz_code": 0, "biz_msg": "", "biz_data": biz_data}}


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


def test_usage_parses_nested_envelope_and_groups_by_api_key(monkeypatch):
    """双层外壳 + 按 API Key 明细；窗口按北京时间 UTC+8，参数只有 start/end/tz。"""
    _setup(monkeypatch, token="ut-test")
    routes = {
        "/user/balance": {"json": _BALANCE},
        "by_api_key/amount": {"json": _envelope({
            "start": 0, "end": 0, "bucket": 86400, "series": [
                {"api_key": "site-agent", "model": "deepseek-flash", "buckets": [
                    {"time": 1, "usage": {"PROMPT_CACHE_HIT_TOKEN": 100,
                                           "PROMPT_CACHE_MISS_TOKEN": 80,
                                           "RESPONSE_TOKEN": 20, "REQUEST": 2}},
                    {"time": 2, "usage": {"RESPONSE_TOKEN": 0, "REQUEST": 70}},
                ]},
                {"api_key": "another-key", "model": "deepseek-flash", "buckets": [
                    {"time": 1, "usage": {"RESPONSE_TOKEN": 50, "REQUEST": 3}},
                ]},
            ]})},
        "by_api_key/cost": {"json": _envelope({
            "data": [{"currency": "CNY", "series": [
                {"api_key": "site-agent", "model": "deepseek-flash",
                 "buckets": [{"time": 1, "cost": "0.32"}]},
                {"api_key": "another-key", "model": "deepseek-flash",
                 "buckets": [{"time": 1, "cost": 0.05}]},
            ]}]})},
        "get_user_summary": {"json": _envelope({
            "total_costs": [{"currency": "CNY", "amount": "296.90"}]})},
    }
    get, calls = _fake_get(routes)
    monkeypatch.setattr(quota.httpx, "get", get)

    usage = quota.check_quota()["usage"]
    assert usage["ok"] is True
    assert usage["tokens"] == 250
    assert usage["requests"] == 75
    assert usage["cost"] == 0.37
    assert usage["total_cost"] == 296.9
    assert usage["currency"] == "CNY"
    assert usage["window_days"] == 30
    assert usage["keys"] == [
        {"name": "site-agent", "cost": 0.32, "requests": 72, "tokens": 200},
        {"name": "another-key", "cost": 0.05, "requests": 3, "tokens": 50},
    ]

    amount_call = next(c for c in calls if "by_api_key/amount" in c["url"])
    assert amount_call["params"]["tz"] == 8 * 3600
    assert "bucket" not in amount_call["params"]     # 平台只认 start/end/tz
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


def test_biz_code_failure_degrades(monkeypatch):
    """外壳 code=0 但 biz_code 非 0 也是业务失败，不能被当成空数据。"""
    _setup(monkeypatch, token="ut-test")
    bad = {"code": 0, "msg": "", "data": {"biz_code": 40003, "biz_msg": "expired",
                                          "biz_data": {}}}
    get, _ = _fake_get({
        "/user/balance": {"json": _BALANCE},
        "by_api_key/amount": {"json": bad},
        "by_api_key/cost": {"json": bad},
        "get_user_summary": {"json": bad},
    })
    monkeypatch.setattr(quota.httpx, "get", get)

    usage = quota.check_quota()["usage"]
    assert usage["ok"] is False
    assert "biz_code=40003" in usage["reason"]


def test_empty_usage_data_is_not_reported_as_zero(monkeypatch):
    """接口 200 但 series 为空 → 明确报空数据，别显示成「近30天 ¥0.00」。"""
    _setup(monkeypatch, token="ut-test")
    get, _ = _fake_get({
        "/user/balance": {"json": _BALANCE},
        "by_api_key/amount": {"json": _envelope({"series": []})},
        "by_api_key/cost": {"json": _envelope({"data": []})},
        "get_user_summary": {"json": _envelope({})},
    })
    monkeypatch.setattr(quota.httpx, "get", get)

    usage = quota.check_quota()["usage"]
    assert usage["ok"] is False
    assert usage["keys"] == []
    assert "空用量" in usage["reason"]


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
    """token 字段改名 + 旧单层外壳都不静默归零。"""
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
