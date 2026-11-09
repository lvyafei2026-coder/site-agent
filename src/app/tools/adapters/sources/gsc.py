# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""Google Search Console：走官方 API，无凭据时 fail-open 返回空列表。

需要：
1. Google Cloud 建服务账号，下载 JSON，填入 GSC_SA_JSON
2. 在 GSC 把服务账号邮箱加为该资源（默认 sc-domain:<FLEET_DOMAIN>）的所有者
3. 启用 Search Console API 与 Indexing API
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Any

from src.app.config import FLEET_DOMAIN

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/indexing",
]


def _credentials():
    from google.oauth2 import service_account

    raw = os.environ.get("GSC_SA_JSON", "")
    if not raw:
        return None
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def _property() -> str:
    # 默认按主域派生 domain 属性；特殊资源（URL 前缀等）用 GSC_PROPERTY 覆盖
    return os.environ.get("GSC_PROPERTY", f"sc-domain:{FLEET_DOMAIN}")


def search_analytics(days: int = 7, dimensions: tuple[str, ...] = ("page",)) -> list[dict[str, Any]]:
    """按维度拉取展示/点击/CTR/排名。无凭据时返回空列表（fail-open）。"""
    creds = _credentials()
    if creds is None:
        log.info("未配置 GSC_SA_JSON，跳过 GSC 数据")
        return []
    try:
        from googleapiclient.discovery import build

        service = build("searchconsole", "v1", credentials=creds, cache_discovery=False)
        end = date.today() - timedelta(days=2)      # GSC 数据有 2 天延迟
        start = end - timedelta(days=days)
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": list(dimensions),
            "rowLimit": 200,
        }
        resp = (
            service.searchanalytics()
            .query(siteUrl=_property(), body=body)
            .execute()
        )
        return resp.get("rows", [])
    except Exception as exc:  # noqa: BLE001 - 采集失败不应阻塞日报
        log.warning("GSC 查询失败：%s", exc)
        return []
