# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""用例四：推广素材与排期（半自动）。

生成周排期与素材草稿，红线由**代码校验器**把关（不依赖 prompt 自觉）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..cf import d1
from ..notify import mail
from ..state import Action, Check, OpsState

log = logging.getLogger(__name__)

NAME = "promo"
LLM_PROFILE = "promo-plan"
PROMPT = "promo"

CHANNELS = ("x", "reddit", "devto", "youtube")
URL_RE = re.compile(r"https?://\S+")

# 红线：写死在这里，不靠 prompt
RULES = {
    "x": "主推不得包含外链",
    "reddit": "新号不得直接贴链接",
    "youtube": "同一音轨不得批量复用",
}


def sense(state: OpsState) -> list[dict[str, Any]]:
    pages = d1.query(
        "SELECT site, slug, keyword, created_at FROM agent_pages "
        "WHERE status IN ('deployed','indexed','verified') "
        "ORDER BY created_at DESC LIMIT 10"
    )
    last_plan = d1.query(
        "SELECT actions FROM agent_reports WHERE kind = 'plan' ORDER BY id DESC LIMIT 1"
    )
    return [
        {
            "week_start": time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400)),
            "new_pages": pages,
            "last_plan": json.loads(last_plan[0]["actions"]) if last_plan else [],
            "channels": list(CHANNELS),
        }
    ]


def build_user_prompt(state: OpsState) -> str:
    obs = state["observations"][0]
    return (
        "为新一周生成推广排期与素材草稿。输出 JSON：plan 数组，每项含 "
        "date（YYYY-MM-DD）、channel（x|reddit|devto|youtube）、asset（shorts|post|article）、"
        "title、draft（发布文案，英文；youtube 则是 15-45 秒口播脚本）、target_url（可空）。\n"
        "约束：x 的 draft 不得出现任何 URL；reddit 不提链接；每条素材必须针对具体页面/数据。\n\n"
        + json.dumps(obs, ensure_ascii=False, indent=2)[:16000]
    )


def validate(item: dict[str, Any]) -> list[str]:
    """返回违规原因列表，空表示通过。"""
    problems: list[str] = []
    channel = item.get("channel")
    draft = item.get("draft", "")

    if channel not in CHANNELS:
        problems.append(f"未知渠道 {channel}")
    if channel == "x" and URL_RE.search(draft):
        problems.append("X 主推含外链（红线）")
    if channel == "reddit" and not item.get("approved_account"):
        problems.append("Reddit 新号不贴链接（红线）")
    if not item.get("title"):
        problems.append("缺标题")
    if len(draft) < 40:
        problems.append("文案过短")
    return problems


def parse(decision: dict[str, Any], state: OpsState) -> list[Action]:
    actions: list[Action] = []
    seen: set[tuple[str, str]] = set()
    for idx, item in enumerate(decision.get("plan", [])):
        problems = validate(item)
        if problems:
            log.warning("排期条目未通过校验：%s | %s", problems, item.get("title"))
            continue
        slot = (item.get("date", ""), item.get("channel", ""))
        if slot in seen:  # 同渠道同一天最多一条主推
            continue
        seen.add(slot)
        actions.append(
            Action(
                type="promo-draft",
                risk="write",
                key=f"promo:{slot[0]}:{slot[1]}:{idx}",
                payload=item,
                summary=f"{slot[0]} {slot[1]}：{item.get('title')}",
            )
        )
    return actions


def execute_action(action: Action) -> dict[str, Any]:
    item = action["payload"]
    today = time.strftime("%Y-%m-%d")
    rows = d1.query(
        "SELECT actions FROM agent_reports WHERE report_date = ? AND kind = 'plan'",
        [today],
    )
    current = json.loads(rows[0]["actions"]) if rows else []
    if any(
        p.get("date") == item.get("date") and p.get("channel") == item.get("channel")
        for p in current
    ):
        return {"key": action["key"], "status": "skipped", "detail": "排期已存在"}
    current.append(item)
    d1.execute(
        """INSERT INTO agent_reports (report_date, kind, summary, anomalies, actions, created_at)
           VALUES (?, 'plan', ?, '[]', ?, ?)
           ON CONFLICT(report_date, kind) DO UPDATE SET actions = excluded.actions""",
        [
            today,
            f"下周排期（{len(current)} 条）",
            json.dumps(current, ensure_ascii=False),
            int(time.time()),
        ],
    )
    return {"key": action["key"], "status": "scheduled", "detail": item.get("title", "")}


def verify(state: OpsState) -> list[Check]:
    scheduled = [e for e in state.get("executed", []) if e.get("status") == "scheduled"]
    if not scheduled:
        return [Check(key="plan", ok=False, detail="本周无有效排期")]
    mail.send(
        f"[site-agent] 本周推广排期（{len(scheduled)} 条）",
        "<pre style='white-space:pre-wrap;font:14px/1.7 -apple-system,sans-serif'>"
        + "\n\n".join(
            f"{e['key']}\n{e.get('detail', '')}" for e in scheduled
        )
        + "</pre>",
    )
    return [Check(key="plan", ok=True, detail=f"{len(scheduled)} 条排期")]


def learn(state: OpsState) -> dict[str, Any]:
    return {
        "task": NAME,
        "scheduled": len([e for e in state.get("executed", []) if e.get("status") == "scheduled"]),
    }
