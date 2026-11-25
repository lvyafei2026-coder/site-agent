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
from .github_repo import git, head_sha

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
               branch: str = "main", timeout_sec: int = 360) -> dict[str, Any]:
    """0 个 check run 时做一次空提交重推（站群实测经验）。"""
    ok = _wait_check_run(repo, sha, timeout_sec=timeout_sec)
    if not ok and work:
        log.warning("构建未成功，空提交重推：%s", repo)
        cwd = Path(work)
        git(cwd, "commit", "--allow-empty", "-m", "ci: retrigger")
        git(cwd, "push", "origin", branch)
        sha = head_sha(cwd)
        ok = _wait_check_run(repo, sha, timeout_sec=timeout_sec)
    return {"ok": ok, "sha": sha,
            "detail": "构建成功" if ok else "CF 构建失败（已重推一次）"}


def verify_live(site: SiteConfig, slug: str, expect: str = "",
                attempts: int = 3, delay_sec: int = 8) -> tuple[bool, str]:
    """上线只认线上页面：带随机参数穿透缓存。

    两个坑一并兜住：
    - **地址口径**：CF Workers 静态资源会把 `/x.html` 307 到 `/x`，所以先打无扩展名
      的规范地址，404 再退 `/x.html`（兼容 GitHub Pages 那类只认扩展名的宿主）。
    - **生效延迟**：推送后构建 1-2 分钟、边缘生效还要几十秒，第一次打过去 404 很常见。
      这里按 attempts×delay 重试，别把「还在构建」判成「上线失败」。
    """
    base = site.url.rstrip("/")
    paths = (slug, f"{slug}.html")
    detail = ""
    for attempt in range(max(1, attempts)):
        for path in paths:
            url = f"{base}/{path}?v={int(time.time())}"
            try:
                resp = requests.get(url, timeout=20)
            except requests.RequestException as exc:
                detail = f"{url} 请求失败: {exc}"
                continue
            if resp.status_code != 200:
                detail = f"{url} 返回 {resp.status_code}"
                continue
            # 200 但内容不含预期标题 = 边缘还挂在旧版本，等下一轮
            if expect and expect[:24] not in resp.text:
                detail = f"{url} 内容未包含预期标题"
                continue
            return True, url
        if attempt + 1 < max(1, attempts):
            time.sleep(delay_sec)
    return False, detail
