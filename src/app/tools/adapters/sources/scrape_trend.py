# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Google Trends 30 天「兴趣度」曲线（从 radar/src/interest.js 原样移植到 Python）。

走 Trends 官方前端自用的两步接口，免 key：
  ① GET /trends/api/explore            → 拿 TIMESERIES widget 的 token + request
  ② GET /trends/api/widgetdata/multiline/csv → 拿逐日 CSV（Day,<词>）
响应体都以 ")]}'\n\n" 开头；explore 是 JSON，csv 是 CSV。

纯函数（strip_xssi / find_timeseries_widget / parse_interest_csv / summarize_interest）
不碰网络，单测直接喂文本。fetch_interest 是网络层，session 可注入。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import requests

from .errors import SourceUnavailable

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://trends.google.com/",
}

TIME_WINDOW = "today 1-m"


def strip_xssi(text: str) -> str:
    t = str(text or "")
    return t[t.index("\n") + 1:] if t.startswith(")]}'") else t


def find_timeseries_widget(json_text: str) -> Optional[dict]:
    try:
        d = json.loads(strip_xssi(json_text))
    except Exception:
        return None
    widgets = d.get("widgets") if isinstance(d, dict) else None
    if not isinstance(widgets, list):
        return None
    for w in widgets:
        if isinstance(w, dict) and w.get("id") == "TIMESERIES" and w.get("token") and w.get("request"):
            return w
    return None


def parse_interest_csv(text: str) -> Optional[list[dict]]:
    out: list[dict] = []
    for line in strip_xssi(text).splitlines():
        m = re.match(r"^(\d{4}-\d{2}-\d{2}),(.*)$", line)
        if not m:
            continue
        v = m.group(2).strip()
        out.append({"d": m.group(1), "v": None if v == "" else (int(float(v)) if v else 0)})
    return out if len(out) >= 5 else None


def summarize_interest(points: list[dict]) -> Optional[dict]:
    vals = [p["v"] for p in (points or []) if isinstance(p, dict) and p.get("v") is not None]
    if not vals:
        return None
    avg = round(sum(vals) / len(vals))
    mx = max(vals)
    last = vals[-1]
    tail = vals[-7:]
    prev = vals[-14:-7]
    delta = None
    if tail and prev:
        a = sum(tail) / len(tail)
        b = sum(prev) / len(prev)
        if b > 0:
            delta = round((a - b) * 100 / b)
    direction = "flat" if delta is None else ("up" if delta >= 15 else "down" if delta <= -15 else "flat")
    return {"avg": avg, "max": mx, "last": last, "delta": delta, "direction": direction}


def fetch_interest(term: str, session: Optional[Any] = None, geo: str = "") -> dict[str, Any]:
    """抓 30 天逐日兴趣度。session 可注入（测试用）。

    返回 {points, summary, attempts}；失败抛异常（带 attempts）。
    """
    import urllib.parse as up
    keyword = str(term or "").strip()
    if not keyword:
        raise ValueError("term required")
    sess = session or requests.Session()
    attempts: list[dict] = []
    last_note = ""

    # 拿 NID cookie（失败不拦，有些节点不种也能直调）；Session 自动带后续请求
    try:
        page = sess.get("https://trends.google.com/trends/explore?geo=US",
                        headers={"User-Agent": UA, "Accept": "text/html"}, timeout=(8, 20))
        attempts.append({"step": "cookie", "status": page.status_code,
                         "note": "NID" if "NID" in (sess.cookies.get("NID") or "") else "无 cookie"})
    except Exception:
        attempts.append({"step": "cookie", "status": 0, "note": "连接失败"})

    req_json = json.dumps(
        {"comparisonItem": [{"keyword": keyword, "geo": geo, "time": TIME_WINDOW}],
         "category": 0, "property": ""}
    )
    headers = dict(BASE_HEADERS)

    for i in range(3):
        if i:
            time.sleep(2.0 if i == 1 else 5.0)
        try:
            resp = sess.get(
                "https://trends.google.com/trends/api/explore?hl=en-US&tz=0&req=" + up.quote(req_json),
                headers=headers, timeout=(8, 20))
        except Exception as exc:
            attempts.append({"step": "explore", "status": 0, "note": "连接失败"})
            last_note = str(exc)
            continue
        attempts.append({"step": "explore", "status": resp.status_code,
                         "note": "" if resp.ok else "HTTP " + str(resp.status_code)})
        if resp.status_code == 429:
            last_note = "Google Trends 限流（429），过几分钟再试"
            continue
        if not resp.ok:
            last_note = "explore HTTP " + str(resp.status_code)
            continue
        widget = find_timeseries_widget(resp.text)
        if not widget:
            last_note = "explore 响应里没有 TIMESERIES（可能被限流）"
            continue
        try:
            csv_resp = sess.get(
                "https://trends.google.com/trends/api/widgetdata/multiline/csv?hl=en-US&tz=0&req="
                + up.quote(json.dumps(widget["request"])) + "&token=" + up.quote(str(widget["token"])),
                headers=headers, timeout=(8, 20))
        except Exception as exc:
            attempts.append({"step": "csv", "status": 0, "note": "连接失败"})
            last_note = str(exc)
            continue
        attempts.append({"step": "csv", "status": csv_resp.status_code,
                         "note": "" if csv_resp.ok else "HTTP " + str(csv_resp.status_code)})
        if not csv_resp.ok:
            last_note = "csv HTTP " + str(csv_resp.status_code)
            continue
        points = parse_interest_csv(csv_resp.text)
        if not points:
            last_note = "CSV 解析为空"
            continue
        return {"points": points, "summary": summarize_interest(points), "attempts": attempts}

    # 3 发全失败：区分「连不上 / 全 429」和「单词解析问题」，都按源级不可用上报——
    # 但上层对趋势的处理是「本批禁用趋势、继续分析」，不像 SERP 那样直接中止整批。
    responded = sum(1 for a in attempts if a.get("step") != "cookie" and a.get("status"))
    if responded == 0:
        note = "连不上 Google Trends（3 发全部连接失败）" + (f"：{last_note}" if last_note else "")
    else:
        note = f"Google Trends 连续 3 发失败（最后一次：{last_note}）"
    raise SourceUnavailable("google-trends", note + "，过几分钟再试", attempts)
