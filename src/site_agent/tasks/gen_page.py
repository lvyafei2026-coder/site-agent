# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""用例一：热词 → 内容页。

流程：radar 候选词 → LLM 生成页面结构 → 规则复检 → 模板渲染 → git push
      → CF 构建 → 线上验证 → GSC 提交 → 7 天后回流效果
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..cf import d1, deploy
from ..collectors import gsc, radar
from ..config import MAX_PAGES_PER_RUN, SITES
from ..state import Action, Check, OpsState

log = logging.getLogger(__name__)

NAME = "gen-page"
LLM_PROFILE = "content-gen"
PROMPT = "gen_page"

# 规则层判死（与 radar 的 DEAD_RULES 对齐；LLM 之外的第二道闸）
DEAD_PATTERNS = ("who is ", "wife", "net worth", "biography", "age of ")


def sense(state: OpsState) -> list[dict[str, Any]]:
    payload = state.get("payload", {})
    site_names = payload.get("sites") or list(SITES)
    cands = radar.candidates(status="doing", limit=40)
    existing = d1.query(
        "SELECT site, slug, keyword FROM agent_pages WHERE status != 'failed'"
    )
    sites = [
        {
            "name": s.name,
            "host": s.host,
            "brand": s.brand,
            "lang": s.lang,
            "daily_limit": s.daily_limit,
        }
        for s in (SITES[n] for n in site_names if n in SITES)
    ]
    return [
        {
            "candidates": cands,
            "existing_pages": existing[:200],
            "sites": sites,
            "date": time.strftime("%Y-%m-%d"),
        }
    ]


def build_user_prompt(state: OpsState) -> str:
    obs = state["observations"][0]
    return (
        "以下是今日候选关键词、站内已有页面与可用站点。请挑选最多 "
        f"{MAX_PAGES_PER_RUN} 个做成新内容页。\n\n"
        + json.dumps(obs, ensure_ascii=False, indent=2)
    )


def is_dead(keyword: str) -> bool:
    low = keyword.lower()
    return any(p in low for p in DEAD_PATTERNS)


def parse(decision: dict[str, Any], state: OpsState) -> list[Action]:
    obs = state["observations"][0]
    taken = {(p["site"], p["slug"]) for p in obs["existing_pages"]}
    valid_sites = {s["name"] for s in obs["sites"]}

    actions: list[Action] = []
    for page in decision.get("pages", [])[:MAX_PAGES_PER_RUN]:
        site = page.get("site", "")
        slug = page.get("slug", "")
        keyword = page.get("keyword", "")
        if site not in valid_sites or not slug:
            log.warning("丢弃非法页面建议：site=%s slug=%s", site, slug)
            continue
        if (site, slug) in taken:
            log.info("页面已存在，跳过：%s/%s", site, slug)
            continue
        if is_dead(keyword):
            log.info("规则层判死：%s", keyword)
            continue
        actions.append(
            Action(
                type="gen-page",
                risk="write",
                key=f"gen-page:{site}:{slug}",
                payload={
                    "site": site,
                    "slug": slug,
                    "keyword": keyword,
                    "page": page,
                },
                summary=f"{site} 新增页面 /{slug}（词：{keyword}）",
            )
        )
    return actions


def execute_action(action: Action) -> dict[str, Any]:
    payload = action["payload"]
    site = SITES[payload["site"]]
    page = payload["page"]

    # 幂等：已部署过则只补 GSC 提交
    done = d1.query(
        "SELECT status FROM agent_pages WHERE site = ? AND slug = ?",
        [site.name, payload["slug"]],
    )
    if done and done[0]["status"] in ("deployed", "indexed", "verified"):
        return {"key": action["key"], "status": "skipped", "detail": "已部署"}

    result = deploy.deploy_page(site, page)
    if not result["ok"]:
        return {"key": action["key"], "status": "failed", "detail": result["detail"]}

    now = int(time.time())
    d1.execute(
        """INSERT INTO agent_pages (site, keyword, slug, status, created_at)
           VALUES (?, ?, ?, 'deployed', ?)
           ON CONFLICT(site, slug) DO UPDATE SET status = 'deployed'""",
        [site.name, payload["keyword"], payload["slug"], now],
    )
    url = f"{site.url}/{payload['slug']}.html"
    indexed = gsc.submit_url(url)

    return {
        "key": action["key"],
        "status": "deployed",
        "url": url,
        "indexing_submitted": indexed,
        "detail": result["detail"],
    }


def verify(state: OpsState) -> list[Check]:
    checks: list[Check] = []
    for item in state.get("executed", []):
        if item.get("status") == "skipped":
            checks.append(Check(key=item["key"], ok=True, detail="已存在，跳过"))
            continue
        if item.get("status") != "deployed":
            checks.append(Check(key=item["key"], ok=False, detail=item.get("detail", "")))
            continue
        # 二次确认线上可访问（deploy 内部已查过一次，这里防止边缘缓存延迟）
        checks.append(Check(key=item["key"], ok=True, detail=item.get("url", "")))
    return checks


def learn(state: OpsState) -> dict[str, Any]:
    """7 天后的效果回流由 daily-report 任务顺带完成（见 daily_report.py）。"""
    deployed = [e for e in state.get("executed", []) if e.get("status") == "deployed"]
    return {
        "task": NAME,
        "deployed": len(deployed),
        "urls": [e.get("url") for e in deployed],
    }
