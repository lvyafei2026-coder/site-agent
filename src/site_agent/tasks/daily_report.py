# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""用例二：数据解读日报（MVP，纯读）。

读 D1 events + GSC → LLM 结构化解读 → 写 agent_reports → 发邮件
顺带完成用例一的"7 天后效果回流"（刷新 agent_pages 的 GSC 指标）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Any

from ..cf import d1
from ..collectors import gsc
from ..notify import mail
from ..state import Action, Check, OpsState

log = logging.getLogger(__name__)

NAME = "daily-report"
LLM_PROFILE = "data-report"
PROMPT = "daily_report"

# ⚠️ 列名以 tool-proxy src/index.js 的 events 表实际 schema 为准，只改这一处
SQL_DAILY = """
SELECT site,
       CAST(ts / 86400000 AS INTEGER) * 86400000 AS day,
       COUNT(*) AS pv,
       COUNT(DISTINCT COALESCE(NULLIF(visitor, ''), ip)) AS uv
FROM events
WHERE ts >= ?
GROUP BY site, day
ORDER BY day DESC, pv DESC
LIMIT 500
"""

SQL_TOP_PAGES = """
SELECT site, path, COUNT(*) AS pv
FROM events
WHERE ts >= ? AND type = 'pageview'
GROUP BY site, path
ORDER BY pv DESC
LIMIT 50
"""


def _since(days: int) -> int:
    return int(time.time() * 1000) - days * 86400_000


def sense(state: OpsState) -> list[dict[str, Any]]:
    try:
        daily = d1.query(SQL_DAILY, [_since(7)])
    except Exception as exc:  # noqa: BLE001 - 表结构或权限问题不阻塞日报
        log.warning("D1 events 查询失败（检查 SQL 列名）：%s", exc)
        daily = []
    try:
        top_pages = d1.query(SQL_TOP_PAGES, [_since(7)])
    except Exception as exc:  # noqa: BLE001
        log.warning("D1 页面榜查询失败：%s", exc)
        top_pages = []

    gsc_rows = gsc.search_analytics(days=7, dimensions=("page",))
    pages = d1.query(
        "SELECT site, slug, keyword, status, created_at FROM agent_pages "
        "WHERE status IN ('deployed','indexed') ORDER BY created_at DESC LIMIT 50"
    )
    return [
        {
            "date": date.today().isoformat(),
            "daily": daily,
            "top_pages": top_pages,
            "gsc": gsc_rows,
            "agent_pages": pages,
        }
    ]


def build_user_prompt(state: OpsState) -> str:
    obs = state["observations"][0]
    return (
        "下面是站群最近 7 天的数据。请输出 JSON，字段："
        "summary（2-3 句总览）、anomalies（异常数组）、"
        "opportunities（机会数组）、actions（建议数组，每项含 "
        "type: content-fix|new-page|promo、site、target、reason、priority: high|mid|low）。\n\n"
        + json.dumps(obs, ensure_ascii=False, indent=2)[:24000]
    )


def parse(decision: dict[str, Any], state: OpsState) -> list[Action]:
    """日报的建议只记录不执行（risk=read），高优先项由人再决定。"""
    actions: list[Action] = []
    for idx, item in enumerate(decision.get("actions", [])[:20]):
        actions.append(
            Action(
                type="suggest",
                risk="read",
                key=f"suggest:{date.today().isoformat()}:{idx}",
                payload={"suggestion": item},
                summary=f"{item.get('site', '')} {item.get('target', '')}：{item.get('reason', '')}",
            )
        )
    return actions


def execute_action(action: Action) -> dict[str, Any]:
    log.info("建议已记录：%s", action["summary"])
    return {"key": action["key"], "status": "recorded", "detail": action["summary"]}


def verify(state: OpsState) -> list[Check]:
    return [Check(key="report", ok=True, detail="报告生成")]


def learn(state: OpsState) -> dict[str, Any]:
    decision = state.get("decision", {})
    today = date.today().isoformat()
    d1.execute(
        """INSERT INTO agent_reports (report_date, kind, summary, anomalies, actions, created_at)
           VALUES (?, 'daily', ?, ?, ?, ?)
           ON CONFLICT(report_date, kind) DO UPDATE SET
             summary = excluded.summary,
             anomalies = excluded.anomalies,
             actions = excluded.actions""",
        [
            today,
            decision.get("summary", ""),
            json.dumps(decision.get("anomalies", []), ensure_ascii=False),
            json.dumps(decision.get("actions", []), ensure_ascii=False),
            int(time.time()),
        ],
    )
    mail.send(f"[site-agent] 每日数据解读 {today}", _render(decision))
    refreshed = refresh_agent_pages()
    return {"task": NAME, "date": today, "refreshed_pages": refreshed}


def _render(decision: dict[str, Any]) -> str:
    def li(items: list[Any], fmt) -> str:
        return "".join(f"<li>{fmt(i)}</li>" for i in items) or "<li>无</li>"

    return f"""
    <div style="font:15px/1.75 -apple-system,sans-serif;color:#111827;max-width:680px">
      <h2 style="font-size:17px;margin:0 0 12px">每日数据解读</h2>
      <p style="color:#374151">{decision.get('summary', '')}</p>
      <h3 style="font-size:14px;margin:20px 0 6px">异常</h3>
      <ul style="margin:0;padding-left:20px">
        {li(decision.get('anomalies', []), lambda x: x if isinstance(x, str) else x.get('detail', str(x)))}
      </ul>
      <h3 style="font-size:14px;margin:20px 0 6px">建议</h3>
      <ul style="margin:0;padding-left:20px">
        {li(decision.get('actions', []), lambda x: f"<b>{x.get('priority', '')}</b> · {x.get('site', '')} · {x.get('reason', '')}")}
      </ul>
    </div>
    """


def refresh_agent_pages() -> int:
    """把已上线页面的 GSC 表现回写到 agent_pages（7 天后开始有数据）。"""
    rows = gsc.search_analytics(days=7, dimensions=("page",))
    if not rows:
        return 0
    updated = 0
    for row in rows:
        page_url = (row.get("keys") or [""])[0]
        if not page_url:
            continue
        slug = page_url.rstrip("/").split("/")[-1].replace(".html", "")
        clicks = int(row.get("clicks", 0))
        for site_name in _sites_from_url(page_url):
            d1.execute(
                """UPDATE agent_pages
                   SET gsc_clicks = ?, gsc_impressions = ?, avg_position = ?,
                       status = CASE WHEN ? > 0 THEN 'verified' ELSE status END,
                       verified_at = ?
                   WHERE site = ? AND slug = ?""",
                [
                    clicks,
                    int(row.get("impressions", 0)),
                    float(row.get("position", 0)),
                    clicks,
                    int(time.time()),
                    site_name,
                    slug,
                ],
            )
            updated += 1
    return updated


def _sites_from_url(url: str) -> list[str]:
    from ..config import SITES

    return [name for name, site in SITES.items() if site.host in url]
