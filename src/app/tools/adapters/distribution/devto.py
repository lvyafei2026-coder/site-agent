# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Dev.to 官方 API：list_articles 只读列热门（无需 key）/ publish 发文。

发文无 DEVTO_API_KEY 时返回 blocked，不抛异常。
"""

from __future__ import annotations

import os
from typing import Any

import requests

from src.app.enums import BacklinkChannel, DevtoStatus


def list_articles(tags: list | None = None) -> list[dict[str, Any]]:
    """按标签取热门文章；单个标签失败只跳过它，不拖垮整次取数。"""
    out = []
    for tag in (tags or ["ai", "webdev", "productivity"]):
        try:
            resp = requests.get(
                "https://dev.to/api/articles",
                params={"tag": tag, "top": 7, "per_page": 5}, timeout=20,
            )
            if resp.status_code != 200:
                continue
            for art in resp.json():
                out.append({
                    "channel": BacklinkChannel.DEVTO.value, "title": art.get("title", ""),
                    "url": art.get("url", ""), "tags": art.get("tag_list", []),
                    "reactions": art.get("public_reactions_count", 0),
                })
        except (requests.RequestException, ValueError):
            continue
    return out


def publish(payload: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("DEVTO_API_KEY", "")
    if not api_key:
        return {"ok": False, "status": DevtoStatus.BLOCKED.value, "detail": "未配置 DEVTO_API_KEY"}
    body = {
        "article": {
            "title": payload.get("title") or payload.get("anchor_text", "")[:80],
            "published": True,
            "body_markdown": payload.get("draft", ""),
            "tags": payload.get("tags") or ["ai", "tools"],
            "canonical_url": payload.get("target_url", ""),
        }
    }
    try:
        resp = requests.post(
            "https://dev.to/api/articles",
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "status": DevtoStatus.FAILED.value, "detail": str(exc)}
    if resp.status_code not in (200, 201):
        return {"ok": False, "status": DevtoStatus.FAILED.value, "detail": resp.text[:200]}
    data = resp.json()
    return {"ok": True, "status": DevtoStatus.PUBLISHED.value, "source_url": data.get("url", ""),
            "detail": "dev.to 已发布"}
