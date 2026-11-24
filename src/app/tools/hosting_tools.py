# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""site setup 域：新子站从零到自动上线的一链四步（定义 + 实现同文件）。

launch_new_site  幂等串四步：GitHub 建仓 scaffold → Cloudflare 建 Worker + Builds 连仓
                 → 绑自定义域 → 双仓配置接线（proxy push 触发 CI、sites.json push 触发
                 Render 重启）。任何一步失败即停（已完成步骤幂等，重跑安全）。

人工前提（一次性，无法 API 化）：Cloudflare GitHub App 授权覆盖新仓库。
上线后页面创建走 publishing 域的 render_files / git_push（既有建页线）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.app.config import SITES, SiteConfig
from src.app.enums import ToolKind
from src.app.tools.adapters.hosting import (
    attach_domain, connect_build, create_site_repo, register_in_proxy,
)
from src.app.tools.base import RuntimeTool


class SiteSetupOps:
    """hosting 域实现：顺序编排四个幂等适配器，失败即停。"""

    def launch_new_site(self, name: str, repo: str = "", brand: str = "",
                        description: str = "", lang: str = "en",
                        paths: list[str] | None = None) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "站点名不能为空（也是子域名，如 invoice）"}
        if name in SITES:
            return {"ok": False, "error": f"站点 {name} 已在站群清单里，不必再建"}
        site = SiteConfig(name=name, repo_name=str(repo or "").strip() or name,
                          subdomain=name, brand=brand.strip() or name, lang=lang)
        steps: dict[str, Any] = {}
        for key, call in (
            ("repo", lambda: create_site_repo(site, description)),
            ("build", lambda: connect_build(site)),
            ("domain", lambda: attach_domain(site)),
            ("proxy", lambda: register_in_proxy(site, paths)),
        ):
            try:
                step = call()
            except Exception as exc:  # noqa: BLE001 - 每步失败都收敛成明文错误
                steps[key] = {"ok": False, "error": str(exc)[:300]}
                return {"ok": False, "site": site.name, "url": site.url,
                        "error": f"步骤 {key} 失败（前序步骤已生效，重跑幂等）",
                        "steps": steps}
            steps[key] = step
        return {"ok": True, "site": site.name, "url": site.url,
                "binding": steps["proxy"].get("binding"), "steps": steps}


class LaunchNewSiteTool(RuntimeTool):
    """上线一个全新子站：建仓 → 连构建 → 绑域名 → 接线双仓配置。幂等，可分步重跑。"""

    tool_name = "launch_new_site"
    method = "launch_new_site"
    kind = ToolKind.WRITE.value

    class Input(BaseModel):
        name: str = Field(description="站点名（同时是子域名，如 invoice → invoice.toolara.dev）")
        repo: str = Field(default="", description="GitHub 仓库名 / Worker 名，缺省同 name")
        brand: str = Field(default="", description="站点品牌名，缺省同 name")
        description: str = Field(default="", description="一句话描述（GitHub 仓库 description）")
        lang: str = Field(default="en", description="页面语言（en/zh…）")
        paths: list[str] = Field(default_factory=lambda: ["/"],
                                 description="可索引路径（tool-proxy sitemap 用），默认 [\"/\"]")
