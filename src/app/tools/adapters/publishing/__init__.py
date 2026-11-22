# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""publishing 域适配器：站点发布流水线（渲染 → 推送 → 构建 → 验活 → 收录）。

消费方：tools/publishing_tools.py。运营通知不在本包——那是独立的
notification 域（adapters/notification/email.py）。

github_repo.py  仓库资源操作：clone / fetch / commit / push（git 子进程集中在此）。
renderer.py     页面模板渲染 + sitemap。
verification.py 构建与上线验活。
"""

from .github_repo import push_files
from .renderer import SLUG_RE, render_files, render_page
from .verification import check_runs, verify_live, wait_build

__all__ = ["SLUG_RE", "render_files", "render_page", "push_files",
           "check_runs", "verify_live", "wait_build"]
