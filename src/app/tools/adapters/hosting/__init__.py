# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""建站适配器：GitHub 仓库 / 渲染 / 构建验活三个独立职责，对外只暴露四个原子步骤。

github_repo.py  就是仓库资源操作：clone / fetch / commit / push（git 子进程集中在此）。
renderer.py     页面模板渲染。
verification.py 构建与上线验活。
"""

from .renderer import SLUG_RE, render_files, render_page
from .github_repo import push_files
from .verification import check_runs, verify_live, wait_build

__all__ = ["SLUG_RE", "render_files", "render_page", "push_files",
           "check_runs", "verify_live", "wait_build"]
