# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Git 仓库操作：clone / fetch / add / commit / push，集中管理子进程调用。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from src.app.config import REPO_DIR, SiteConfig, env

os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def head_sha(work: Path) -> str:
    return git(work, "rev-parse", "HEAD")


def has_staged_changes(work: Path) -> bool:
    return subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=work
    ).returncode != 0


def ensure_repo(site: SiteConfig) -> Path:
    work = REPO_DIR / site.repo_name
    if (work / ".git").exists():
        git(work, "fetch", "--depth", "1", "origin", site.branch)
        git(work, "reset", "--hard", f"origin/{site.branch}")
        return work
    work.parent.mkdir(parents=True, exist_ok=True)
    token = env("GITHUB_TOKEN", required=True)
    url = f"https://x-access-token:{token}@github.com/{site.repo}.git"
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", site.branch, url, str(work)],
        check=True, capture_output=True, timeout=300,
    )
    return work


def push_files(site: SiteConfig, page: dict[str, Any], work: str | Path) -> dict[str, Any]:
    """commit + push。无变更时不重复提交。不可逆。"""
    cwd = Path(work)
    if not has_staged_changes(cwd):
        return {"ok": True, "skipped": True, "sha": head_sha(cwd),
                "repo": site.repo, "branch": site.branch, "detail": "无变更，跳过提交"}
    slug = page.get("slug", "page")
    git(cwd, "commit", "-m", f"add: {slug}")
    git(cwd, "push", "origin", site.branch)
    return {"ok": True, "skipped": False, "sha": head_sha(cwd),
            "repo": site.repo, "branch": site.branch, "detail": f"已推送 {slug}"}
