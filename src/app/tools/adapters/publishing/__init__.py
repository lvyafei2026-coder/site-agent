# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""publishing 域适配器：站点发布流水线 + 运营通知。

消费方：tools/publishing_tools.py（渲染 / 推送 / 验活）、
tools/ledger_tools.py 的 send_notice（通知也走对外发出通道）。

github_repo.py  仓库资源操作：clone / fetch / commit / push（git 子进程集中在此）。
renderer.py     页面模板渲染 + sitemap。
verification.py 构建与上线验活。
email.py        Resend 发信（日报 / 告警 / 结果摘要），不可逆。
"""

from . import email
from .github_repo import push_files
from .renderer import SLUG_RE, render_files, render_page
from .verification import check_runs, verify_live, wait_build

__all__ = ["SLUG_RE", "render_files", "render_page", "push_files",
           "check_runs", "verify_live", "wait_build", "email"]
