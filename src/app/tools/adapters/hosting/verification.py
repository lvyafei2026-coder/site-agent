# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""构建与上线验证：GitHub check run + 线上页面穿透缓存验活。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests

from src.app.config import SiteConfig, env
from .repository import git, head_sha

log = logging.getLogger(__name__)


def check_runs(repo: str, sha: str) -> list[dict[str, Any]]:
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs",
        headers={
            "Authorization": f"Bearer {env('GITHUB_TOKEN', required=True)}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("check_runs", [])


def _wait_check_run(repo: str, sha: str, timeout_sec: int = 360) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        runs = [r for r in check_runs(repo, sha) if "Workers Builds" in r.get("name", "")]
        if runs and all(r.get("status") == "completed" for r in runs):
            return all(r.get("conclusion") == "success" for r in runs)
        time.sleep(15)
    return False


def wait_build(repo: str, sha: str, work: str = "",
               branch: str = "main") -> dict[str, Any]:
    """0 个 check run 时做一次空提交重推（站群实测经验）。"""
    ok = _wait_check_run(repo, sha)
    if not ok and work:
        log.warning("构建未成功，空提交重推：%s", repo)
        cwd = Path(work)
        git(cwd, "commit", "--allow-empty", "-m", "ci: retrigger")
        git(cwd, "push", "origin", branch)
        sha = head_sha(cwd)
        ok = _wait_check_run(repo, sha)
    return {"ok": ok, "sha": sha,
            "detail": "构建成功" if ok else "CF 构建失败（已重推一次）"}


def verify_live(site: SiteConfig, slug: str, expect: str) -> tuple[bool, str]:
    """上线只认线上页面：带随机参数穿透缓存。"""
    url = f"{site.url}/{slug}.html?v={int(time.time())}"
    try:
        resp = requests.get(url, timeout=20)
    except requests.RequestException as exc:
        return False, f"{url} 请求失败: {exc}"
    if resp.status_code != 200:
        return False, f"{url} 返回 {resp.status_code}"
    if expect and expect[:24] not in resp.text:
        return False, f"{url} 内容未包含预期标题"
    return True, url
