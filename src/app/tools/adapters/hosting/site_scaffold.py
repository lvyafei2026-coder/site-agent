# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""GitHub 侧建站：REST 建仓 + scaffold 首推（目录名 = 仓库名 = Worker 名）。

scaffold 是既有 32 站的最小公约数：wrangler.toml（assets=./public）+ package.json
（Workers Builds 的 build/deploy 命令需要它）+ public/index.html + public/sitemap.xml。
robots.txt / sitemap.xml 的正式版由 tool-proxy 代理层动态生成，这里只是兜底文件。

幂等：仓库已存在 → 直接复用（不重建、不覆盖任何远端内容）；本地工作副本已存在 →
只补缺失的 scaffold 文件再推。
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import requests

from src.app.config import CONTACT_EMAIL, REPO_DIR, SiteConfig, env

os_env = __import__("os").environ
os_env.setdefault("GIT_TERMINAL_PROMPT", "0")

GH_API = "https://api.github.com"


def github_rest(method: str, path: str, payload: dict | None = None) -> dict:
    """GitHub REST 统一入口：返回 {status, body}。凭据 = GITHUB_TOKEN。"""
    resp = requests.request(
        method, GH_API + path,
        headers={"Authorization": f"Bearer {env('GITHUB_TOKEN', required=True)}",
                 "Accept": "application/vnd.github+json"},
        json=payload, timeout=30,
    )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return {"status": resp.status_code, "body": body}


def repo_info(repo_full: str) -> dict | None:
    """查仓库（含 id——Workers Builds 连仓要用 GitHub 数字 id）。不存在返回 None。"""
    out = github_rest("GET", f"/repos/{repo_full}")
    return out["body"] if out["status"] == 200 else None


def create_repo(repo_full: str, description: str) -> dict[str, Any]:
    """幂等建仓：已存在直接复用；不存在才 POST。返回 {id, full_name, created}。"""
    existing = repo_info(repo_full)
    if existing:
        return {"ok": True, "created": False, "id": existing["id"],
                "full_name": existing["full_name"],
                "html_url": existing["html_url"]}
    owner, name = repo_full.split("/", 1)
    out = github_rest("POST", "/user/repos", {
        "name": name, "description": description[:350],
        "private": False, "auto_init": False,
    })
    if out["status"] not in (201, 202):
        raise RuntimeError(f"GitHub 建仓失败 {repo_full}: {out['body'].get('message', out)}")
    return {"ok": True, "created": True, "id": out["body"]["id"],
            "full_name": out["body"]["full_name"],
            "html_url": out["body"]["html_url"]}


# ---- scaffold ----

def _wrangler_toml(repo_name: str) -> str:
    return (f'name = "{repo_name}"\n'
            f'compatibility_date = "{date.today().isoformat()}"\n'
            "\n[assets]\ndirectory = \"./public\"\n")


def _package_json(repo_name: str, description: str) -> str:
    return json.dumps({
        "name": repo_name, "private": True,
        "description": description[:200] or f"{repo_name} static site",
        "scripts": {"build": "echo \"Static site, no build step\"",
                    "deploy": "wrangler deploy"},
        "devDependencies": {},
    }, indent=2, ensure_ascii=False) + "\n"


def _index_html(site: SiteConfig) -> str:
    esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="{site.lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(site.brand)}</title>
<meta name="description" content="{esc(site.brand)} — free online tool on {site.host}.">
<link rel="canonical" href="https://{site.host}/">
</head>
<body>
<main>
  <h1>{esc(site.brand)}</h1>
  <p>{esc(site.brand)} is part of the Toolara toolbox. Content pages will appear here.</p>
</main>
</body>
</html>
"""


def _sitemap_xml(site: SiteConfig) -> str:
    return (f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://{site.host}/</loc>
    <lastmod>{date.today().isoformat()}</lastmod>
    <priority>1.0</priority>
  </url>
</urlset>
""")


def write_scaffold(work: Path, site: SiteConfig, description: str) -> list[str]:
    """补齐缺失的 scaffold 文件（已存在的一律不动，绝不覆盖站点自己的内容）。"""
    written: list[str] = []
    public = work / site.asset_dir
    public.mkdir(parents=True, exist_ok=True)
    files = {
        work / "wrangler.toml": _wrangler_toml(site.repo_name),
        work / "package.json": _package_json(site.repo_name, description),
        public / "index.html": _index_html(site),
        public / "sitemap.xml": _sitemap_xml(site),
    }
    for path, text in files.items():
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            written.append(str(path.relative_to(work)))
    return written


def init_and_push(work: Path, repo_full: str, branch: str) -> str:
    """空仓/新工作副本的首次提交推送（git 子进程只在 publishing.github_repo 之外的这里出现一次）。"""
    token = env("GITHUB_TOKEN", required=True)
    url = f"https://x-access-token:{token}@github.com/{repo_full}.git"
    def run(*args: str) -> None:
        cmd = ["git"]
        if args and args[0] == "commit":
            # Render 容器没有全局 git 身份；commit 必须显式带，否则整条推送链挂掉。
            cmd += ["-c", f"user.name={env('GIT_AUTHOR_NAME', 'site-agent')}",
                    "-c", f"user.email={env('GIT_AUTHOR_EMAIL', CONTACT_EMAIL)}"]
        cmd += [*args]
        proc = subprocess.run(cmd, cwd=str(work),
                              capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:400]}")
    run("init", "-b", branch)
    run("remote", "add", "origin", url)
    run("add", "-A")
    run("commit", "-m", "init: scaffold static site")
    run("push", "-u", "origin", branch)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(work),
                          capture_output=True, text=True, timeout=30).stdout.strip()


def create_site_repo(site: SiteConfig, description: str = "") -> dict[str, Any]:
    """建站第一步：GitHub 建仓（幂等）+ scaffold 首推。返回 {repo, url, sha, scaffold}。"""
    created = create_repo(site.repo, description or f"{site.brand} static site")
    work = REPO_DIR / site.repo_name
    scaffold: list[str] = []
    if (work / ".git").exists():
        # 已有工作副本：只补缺失文件（本地副本本来就是我们推过的）
        scaffold = write_scaffold(work, site, description)
    else:
        work.parent.mkdir(parents=True, exist_ok=True)
        work.mkdir(exist_ok=True)
        scaffold = write_scaffold(work, site, description)
        init_and_push(work, site.repo, site.branch)
    return {"ok": True, "repo": created["full_name"], "url": created["html_url"],
            "created": created["created"], "scaffold": scaffold}
