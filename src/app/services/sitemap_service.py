# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""建页后的代理 sitemap 登记：节点只调本层，不碰 adapter / gitIO。

线上 sitemap 由 tool-proxy 的 SITE_PATHS 生成（站点仓静态文件已删），所以
「页面上线」和「页面可被收录」是两个仓库的两步。登记失败只告警、不回滚已上线页面，
结果随建页结果返回，方便在 /agent 面板看到。
"""

from __future__ import annotations

import logging
from typing import Any

from src.app.config import SITES
from src.app.tools.adapters.hosting.proxy_config import register_page_in_proxy

log = logging.getLogger(__name__)


def register_page(site_name: str, slug: str) -> dict[str, Any]:
    """把新页规范 URL 登记进 tool-proxy；幂等，失败 fail-open。"""
    site = SITES.get(site_name)
    if not site or not slug:
        return {"ok": False, "detail": f"无法登记代理 sitemap：站点={site_name!r} slug={slug!r}"}
    try:
        return register_page_in_proxy(site, slug)
    except Exception as exc:
        log.warning("proxy sitemap registration failed for %s/%s: %s", site_name, slug, exc)
        return {"ok": False, "detail": f"代理 sitemap 登记失败：{exc}"}
