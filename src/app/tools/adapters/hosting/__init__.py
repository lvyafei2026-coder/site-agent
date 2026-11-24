# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""hosting 域适配器：新子站从零到上线的三段外部动作（与 publishing 的"内容推送"分开）。

site_scaffold.py  GitHub：scaffold 仓库 + REST 建仓 + 首推（目录名 = 仓库名 = Worker 名）。
deploy.py         Cloudflare：建 Worker → Workers Builds 连仓 → 绑自定义域（自动建 DNS）。
proxy_config.py   双仓配置：tool-proxy（sites.js + wrangler.toml）+ site-agent（config/sites.json）。

上线后与 publishing 域汇合：后续页面 = render_files → push_files → Workers Builds 自动部署。
一次性人工前提（无法 API 化）：Cloudflare GitHub App 授权（含新仓库访问权限）。
"""

from .deploy import attach_domain, connect_build, create_worker
from .proxy_config import register_in_proxy
from .site_scaffold import create_site_repo

__all__ = ["create_site_repo", "create_worker", "connect_build",
           "attach_domain", "register_in_proxy"]
