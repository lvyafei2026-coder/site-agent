# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Dev.to 官方发布驱动：无 DEVTO_API_KEY 时返回 blocked，不抛异常。"""

from __future__ import annotations

import os
from typing import Any

import requests


def publish(payload: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("DEVTO_API_KEY", "")
    if not api_key:
        return {"ok": False, "status": "blocked", "detail": "未配置 DEVTO_API_KEY"}
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
        return {"ok": False, "status": "failed", "detail": str(exc)}
    if resp.status_code not in (200, 201):
        return {"ok": False, "status": "failed", "detail": resp.text[:200]}
    data = resp.json()
    return {"ok": True, "status": "published", "source_url": data.get("url", ""),
            "detail": "dev.to 已发布"}
