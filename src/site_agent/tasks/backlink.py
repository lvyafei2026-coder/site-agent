# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""用例三：外链机会发现（半自动）。

只做"发现 + 评分 + 草稿"；Reddit / 目录站一律人工发，Dev.to 走官方 API 可全自动。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

from ..cf import d1
from ..collectors import reddit
from ..notify import mail
from ..state import Action, Check, OpsState

log = logging.getLogger(__name__)

NAME = "backlink"
LLM_PROFILE = "backlink-score"
PROMPT = "backlink"

# 渠道权重：devto 可自动化；reddit 需人工；directory 已封盘只记录
CHANNEL_WEIGHT = {"devto": 1.0, "reddit": 0.8, "directory": 0.5}
SCORE_THRESHOLD = 0.7
SEARCH_QUERIES = [
    "best ai music generator",
    "free ai tool for students",
    "how to remove background from photo",
]
DEVTO_TAGS = ["ai", "webdev", "productivity"]


def sense(state: OpsState) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for q in SEARCH_QUERIES:
        threads.extend(reddit.search(q, limit=8))

    devto: list[dict[str, Any]] = []
    for tag in DEVTO_TAGS:
        try:
            resp = requests.get(
                "https://dev.to/api/articles",
                params={"tag": tag, "top": 7, "per_page": 5},
                timeout=20,
            )
            for art in resp.json() if resp.status_code == 200 else []:
                devto.append(
                    {
                        "channel": "devto",
                        "title": art.get("title", ""),
                        "url": art.get("url", ""),
                        "tags": art.get("tag_list", []),
                        "reactions": art.get("public_reactions_count", 0),
                    }
                )
        except (requests.RequestException, ValueError) as exc:
            log.warning("dev.to 读取失败：%s", exc)

    existing = d1.query(
        "SELECT channel, source_url FROM agent_backlinks WHERE status != 'dead' LIMIT 200"
    )
    return [{"threads": threads[:30], "devto": devto[:20], "existing": existing}]


def build_user_prompt(state: OpsState) -> str:
    obs = state["observations"][0]
    return (
        "下面是可投放外链的机会（Reddit 讨论 + Dev.to 文章）。请评估哪些真的适合我们"
        "（工具站 toolara.dev，免费 AI 工具），输出 JSON：opportunities 数组，每项含 "
        "channel（devto|reddit）、target_url（我们被链接的页面）、source_url（机会所在页）、"
        "anchor_text、score（0-1）、reason、draft（可直接发布的文案，英文）。\n\n"
        + json.dumps(obs, ensure_ascii=False, indent=2)[:20000]
    )


def parse(decision: dict[str, Any], state: OpsState) -> list[Action]:
    obs = state["observations"][0]
    taken = {(e["channel"], e.get("source_url")) for e in obs["existing"]}

    actions: list[Action] = []
    for item in decision.get("opportunities", []):
        channel = item.get("channel", "")
        score = float(item.get("score", 0))
        if channel not in CHANNEL_WEIGHT or score < SCORE_THRESHOLD:
            continue
        if (channel, item.get("source_url")) in taken:
            continue
        actions.append(
            Action(
                type="backlink",
                risk="write",
                key=f"backlink:{channel}:{item.get('target_url')}:{abs(hash(item.get('source_url', ''))) % 10**8}",
                payload={**item, "score": score},
                summary=f"{channel} 机会 score={score:.2f}：{item.get('reason', '')[:80]}",
                # Dev.to 有官方 API，属白名单渠道，无需人工二次确认
                auto=(channel == "devto"),
            )
        )
    return actions


def execute_action(action: Action) -> dict[str, Any]:
    item = action["payload"]
    channel = item.get("channel")

    if channel == "devto":
        return _publish_devto(action)

    # Reddit / 目录站：审批通过后仍由人发，这里只把定稿推到邮箱
    mail.send(
        f"[site-agent] 外链草稿已批准：{channel}",
        f"<pre style='white-space:pre-wrap;font:14px/1.7 -apple-system,sans-serif'>"
        f"{item.get('draft', '')}\n\n→ {item.get('target_url', '')}</pre>",
    )
    return {"key": action["key"], "status": "ready-to-post", "detail": channel}


def _publish_devto(action: Action) -> dict[str, Any]:
    api_key = os.environ.get("DEVTO_API_KEY", "")
    if not api_key:
        return {"key": action["key"], "status": "blocked", "detail": "未配置 DEVTO_API_KEY"}
    item = action["payload"]
    body = {
        "article": {
            "title": item.get("title") or item.get("anchor_text", "")[:80],
            "published": True,
            "body_markdown": item.get("draft", ""),
            "tags": ["ai", "tools"],
            "canonical_url": item.get("target_url", ""),
        }
    }
    try:
        resp = requests.post(
            "https://dev.to/api/articles",
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            return {"key": action["key"], "status": "failed", "detail": resp.text[:200]}
        data = resp.json()
        return {
            "key": action["key"],
            "status": "published",
            "source_url": data.get("url", ""),
            "detail": "dev.to 已发布",
        }
    except (requests.RequestException, ValueError) as exc:
        return {"key": action["key"], "status": "failed", "detail": str(exc)}


def verify(state: OpsState) -> list[Check]:
    checks = []
    for item in state.get("executed", []):
        ok = item.get("status") in ("published", "ready-to-post")
        checks.append(Check(key=item["key"], ok=ok, detail=item.get("detail", "")))
    return checks


def learn(state: OpsState) -> dict[str, Any]:
    now = int(time.time())
    count = 0
    for item in state.get("executed", []):
        if item.get("status") not in ("published", "ready-to-post"):
            continue
        action = next((a for a in state.get("actions", []) if a["key"] == item["key"]), None)
        if not action:
            continue
        payload = action["payload"]
        d1.execute(
            """INSERT INTO agent_backlinks
               (channel, target_url, source_url, anchor_text, status, score, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                payload.get("channel"),
                payload.get("target_url"),
                item.get("source_url") or payload.get("source_url"),
                payload.get("anchor_text"),
                "published" if item["status"] == "published" else "approved",
                payload.get("score"),
                now,
            ],
        )
        count += 1
    return {"task": NAME, "recorded": count}
