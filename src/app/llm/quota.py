# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""LLM 额度与用量查询：DeepSeek 官方余额 + 可选的平台用量面板数据。

两条数据源，凭据完全不同，别混：

1. 余额：``GET https://api.deepseek.com/user/balance``，用 ``LLM_API_KEY``。
   这是官方**公开**账户接口，不消耗推理额度、不占请求次数，返回裸 JSON
   ``is_available`` + ``balance_infos[]``（total / topped_up / granted）。
2. 近 30 天消费、API 请求次数、Tokens、累计消费：``platform.deepseek.com``
   后台「用量信息」页的私有面板接口，**官方没有公开用量 API**（api.deepseek.com
   除 balance 外全是 404）。它需要浏览器登录态 ``userToken``，即环境变量
   ``DEEPSEEK_PLATFORM_TOKEN``，不是 API key。私有接口随时可能变；登录态失效
   时返回 ``code`` 40002/40003，这里降级成可读提示，绝不影响余额显示。

★平台响应是双层外壳，别再按单层解析（11-16 实测踩过）：

    {"code":0,"msg":"","data":{"biz_code":0,"biz_msg":"","biz_data":{...}}}

业务体在 ``data.biz_data``；``code`` 与 ``biz_code`` 任一非 0 都算失败。
``/usage/by_api_key/amount`` 的 ``biz_data.series[]`` 按 api_key × model 分组，
``/usage/by_api_key/cost`` 的 ``biz_data.data[].series[]`` 亦然，请求参数只有
``start`` / ``end``（秒级 epoch）/ ``tz``（时区偏移秒），**没有 bucket 参数**。

展示口径：窗口与时间一律北京时间 UTC+8（与后台一致），后台数据约 5 分钟延迟。
页面加载时自动查一次，不做 cron、不缓存；金额/Tokens 同时给出账户合计与
按 API Key 的明细（``usage.keys[]``），卡片主数字取 ``DEEPSEEK_API_KEY_NAME``
指定的那个 key（``usage.current``），避免把其它调用方的消费算进来。
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from src.app.config import (
    DEEPSEEK_API_KEY_NAME,
    DEEPSEEK_PLATFORM_TOKEN,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
)

_DEFAULT_BASE = "https://api.deepseek.com"
_PLATFORM_BASE = "https://platform.deepseek.com"
_BJ = timezone(timedelta(hours=8))
_TIMEOUT = 20.0
_WINDOW_DAYS = 30
_TOKEN_KEYS = ("PROMPT_CACHE_HIT_TOKEN", "PROMPT_CACHE_MISS_TOKEN", "RESPONSE_TOKEN")
_OK_CODES = (None, 0, "0", 200, "200")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_bj() -> str:
    return datetime.now(_BJ).strftime("%m-%d %H:%M")


def _num(value) -> float:
    """面板里的金额/token 可能是字符串，也可能是 null；一律容错成 0。"""
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _get_json(url: str, *, headers: dict, params: dict | None = None) -> dict:
    """GET 并拆掉平台双层响应外壳；code / biz_code 非 0 视为失败。

    官方 /user/balance 是裸 JSON（无 code/data 外壳），原样返回，别硬套平台格式。
    """
    resp = httpx.get(url, headers=headers, params=params, timeout=_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError("响应不是 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("响应结构异常")

    code = data.get("code")
    if code not in _OK_CODES:
        msg = data.get("msg") or data.get("message") or ""
        raise RuntimeError(
            f"平台返回 code={code} {msg}（登录态可能已失效，需重新取 userToken）")

    envelope = data.get("data")
    if isinstance(envelope, dict):
        biz_code = envelope.get("biz_code")
        if biz_code not in _OK_CODES:
            msg = envelope.get("biz_msg") or envelope.get("msg") or ""
            raise RuntimeError(
                f"平台返回 biz_code={biz_code} {msg}（登录态可能已失效，需重新取 userToken）")
        inner = envelope.get("biz_data")
        if isinstance(inner, dict):
            return inner
        return envelope                       # 兼容早期扁平结构

    inner = data.get("biz_data")                # 兼容更老的单层外壳
    if isinstance(inner, dict):
        return inner
    return data                                # 官方余额接口：裸 JSON


def _balance() -> dict:
    """官方公开余额接口；非 DeepSeek 网关明确报「不支持」，不瞎猜路径。"""
    base = (LLM_BASE_URL or _DEFAULT_BASE).rstrip("/")
    if "deepseek.com" not in base.lower():
        return {"ok": False,
                "reason": f"当前网关 {urlparse(base).netloc} 不支持官方余额接口"}
    try:
        payload = _get_json(
            base + "/user/balance",
            headers={"Authorization": f"Bearer {LLM_API_KEY}",
                     "Accept": "application/json"},
        )
    except Exception as exc:
        return {"ok": False, "reason": f"余额查询失败：{exc}"}

    infos = payload.get("balance_infos") or []
    pick = next((i for i in infos
                 if isinstance(i, dict) and str(i.get("currency", "")).upper() == "CNY"), None)
    if pick is None:
        pick = next((i for i in infos if isinstance(i, dict)), None)
    if pick is None:
        return {"ok": False, "reason": "余额响应缺少 balance_infos"}
    return {
        "ok": True,
        "available": bool(payload.get("is_available")),
        "currency": str(pick.get("currency") or "CNY"),
        "total": str(pick.get("total_balance") or "0"),
        "topped_up": str(pick.get("topped_up_balance") or "0"),
        "granted": str(pick.get("granted_balance") or "0"),
    }


def _window() -> tuple[int, int]:
    """后台「近 30 天」= 北京时间今天往前 29 天的 00:00 → 明天 00:00。"""
    today = datetime.now(_BJ).date()
    start = datetime.combine(today - timedelta(days=_WINDOW_DAYS - 1), dtime.min, tzinfo=_BJ)
    end = datetime.combine(today + timedelta(days=1), dtime.min, tzinfo=_BJ)
    return int(start.timestamp()), int(end.timestamp())


def _key_name(series: dict) -> str:
    """series 的 api_key 可能是名称字符串，也可能是对象；统一成展示名。"""
    raw = series.get("api_key") or series.get("api_key_name") \
        or series.get("key_name") or series.get("name")
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("api_key") or raw.get("id")
    return str(raw or "未命名 key")


def _sum_amount(payload: dict) -> dict[str, dict]:
    """/usage/by_api_key/amount：按 API Key 累加 tokens 与请求次数。"""
    keys: dict[str, dict] = {}
    for series in payload.get("series") or []:
        if not isinstance(series, dict):
            continue
        slot = keys.setdefault(_key_name(series), {"tokens": 0.0, "requests": 0.0})
        for bucket in series.get("buckets") or []:
            usage = (bucket or {}).get("usage") or {}
            if not isinstance(usage, dict):
                continue
            slot["requests"] += _num(usage.get("REQUEST"))
            hits = [_num(usage[k]) for k in _TOKEN_KEYS if k in usage]
            if hits:
                slot["tokens"] += sum(hits)
            else:  # 字段命名变了也别静默归零：除 REQUEST 外的数值都算 token
                slot["tokens"] += sum(_num(v) for k, v in usage.items() if k != "REQUEST")
    return keys


def _sum_cost(payload: dict) -> tuple[dict[str, float], str]:
    """/usage/by_api_key/cost：data[].series[].buckets[].cost 按 API Key 累加。"""
    costs: dict[str, float] = {}
    currency = ""
    entries = payload.get("data")
    if not isinstance(entries, list):
        entries = payload.get("series") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cur = entry.get("currency")
        if cur and not currency:
            currency = str(cur)
        series_list = entry.get("series")
        if not isinstance(series_list, list):
            series_list = [entry]             # 兼容扁平结构
        for series in series_list:
            if not isinstance(series, dict):
                continue
            name = _key_name(series)
            for bucket in series.get("buckets") or []:
                costs[name] = costs.get(name, 0.0) + _num((bucket or {}).get("cost"))
            if "buckets" not in series and "cost" in series:
                costs[name] = costs.get(name, 0.0) + _num(series.get("cost"))
    return costs, currency


def _summary_total_cost(summary: dict):
    """/users/get_user_summary 的累计消费；拿不到返回 None（前端不显示该项）。"""
    rows = summary.get("total_costs") or []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                value = row.get("amount", row.get("cost"))
                if value not in (None, ""):
                    return round(_num(value), 2)
    for key in ("total_usage", "total_cost"):
        value = summary.get(key)
        if isinstance(value, (int, float, str)) and value != "":
            return round(_num(value), 2)
    return None


def _platform_usage() -> dict:
    """平台私有面板用量；未配登录态 token 时明确降级，余额照常返回。"""
    token = (DEEPSEEK_PLATFORM_TOKEN or "").strip()
    if not token:
        return {"ok": False,
                "reason": "未配置 DEEPSEEK_PLATFORM_TOKEN：消费/请求数/Tokens 需平台登录态"}

    start, end = _window()
    params = {"start": start, "end": end, "tz": 8 * 3600}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    out: dict = {"ok": False, "window_days": _WINDOW_DAYS, "currency": "CNY",
                 "note": "北京时间，后台数据约 5 分钟延迟"}
    errors: dict[str, str] = {}
    amount_by_key: dict[str, dict] = {}
    cost_by_key: dict[str, float] = {}

    try:
        amount = _get_json(_PLATFORM_BASE + "/api/v0/usage/by_api_key/amount",
                           headers=headers, params=params)
        amount_by_key = _sum_amount(amount)
        amount_ok = True
    except Exception as exc:
        errors["amount"] = str(exc)
        amount_ok = False

    try:
        cost = _get_json(_PLATFORM_BASE + "/api/v0/usage/by_api_key/cost",
                         headers=headers, params=params)
        cost_by_key, currency = _sum_cost(cost)
        if currency:
            out["currency"] = currency
        cost_ok = True
    except Exception as exc:
        errors["cost"] = str(exc)
        cost_ok = False

    try:
        summary = _get_json(_PLATFORM_BASE + "/api/v0/users/get_user_summary",
                            headers=headers)
        total_cost = _summary_total_cost(summary)
        if total_cost is not None:
            out["total_cost"] = total_cost
    except Exception as exc:
        errors["summary"] = str(exc)

    names = set(amount_by_key) | set(cost_by_key)
    keys = [{
        "name": name,
        "cost": round(cost_by_key.get(name, 0.0), 4),
        "requests": int(amount_by_key.get(name, {}).get("requests", 0)),
        "tokens": int(amount_by_key.get(name, {}).get("tokens", 0)),
    } for name in names]
    keys.sort(key=lambda k: (-k["cost"], -k["requests"], k["name"]))
    out["keys"] = keys
    wanted = (DEEPSEEK_API_KEY_NAME or "").strip().lower()
    current = next((k for k in keys if wanted and k["name"].lower() == wanted), None)
    if current:
        out["current_key"] = current["name"]
        out["current"] = current          # 卡片主数字只认本站这个 key
    out["cost"] = round(sum(cost_by_key.values()), 4)
    out["requests"] = sum(k["requests"] for k in keys)
    out["tokens"] = sum(k["tokens"] for k in keys)

    out["ok"] = bool(amount_ok and cost_ok and keys)
    if errors:
        out["errors"] = errors
    if not out["ok"]:
        if errors:
            out["reason"] = "平台用量查询失败：" + "；".join(
                f"{k}:{v}" for k, v in errors.items())
        else:
            out["reason"] = "平台返回空用量数据（登录态可能已失效，或窗口内确实无调用）"
    return out


def check_quota() -> dict:
    """组合结果；balance / usage 各自独立 ok，一项挂了不影响另一项展示。

    未配 LLM_API_KEY 时整体短路（余额是核心数据，且 /llm-quota 的历史契约
    要求此时 ok=False）。
    """
    if not LLM_API_KEY:
        return {"ok": False, "reason": "LLM_API_KEY 未配置，无法查询额度"}

    balance = _balance()
    usage = _platform_usage()
    ok = bool(balance.get("ok") or usage.get("ok"))
    base = (LLM_BASE_URL or _DEFAULT_BASE).rstrip("/")
    result = {
        "ok": ok,
        "model": LLM_MODEL,
        "endpoint": urlparse(base).netloc,
        "available": bool(balance.get("available")),
        "balance": balance,
        "usage": usage,
        "quota": {},                 # 兼容旧前端的 ratelimit 头字段
        "checked_at": _now(),
        "checked_at_bj": _now_bj(),
    }
    if not ok:
        result["reason"] = balance.get("reason") or usage.get("reason") or "额度查询失败"
    return result
