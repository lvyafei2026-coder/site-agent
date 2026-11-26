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

from src.app.config import CONTACT_EMAIL, FLEET_DOMAIN, REPO_DIR, SiteConfig, env

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


# 首页文章列表区块：renderer 建页后按这两个标记重建卡片，别的地方别动。
PAGES_START = "<!-- AGENT_PAGES:START -->"
PAGES_END = "<!-- AGENT_PAGES:END -->"

# 新站全局样式：首页和后续生成的内页都引用它（renderer 从 index.html 抽 stylesheet 继承），
# 一处定义整站统一。变量的命名沿用 --ink/--accent/--line，兼容 renderer 的旧 skin 兜底。
SITE_CSS = """\
:root{color-scheme:light dark;--bg:#f7f8fc;--card:#ffffff;--ink:#101828;--ink-soft:#475467;--muted:#667085;--line:#e6eaf2;--accent:#3b5bdb;--accent-soft:#eef2ff;--shadow:0 12px 32px rgba(16,24,40,.06);--radius:18px}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.site-header{position:sticky;top:0;z-index:10;background:rgba(255,255,255,.9);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.header-inner{max-width:960px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;gap:14px}
.logo{font-weight:750;letter-spacing:-.02em;font-size:1.08rem;color:var(--ink)}
.logo:hover{text-decoration:none}
.logo-dot{color:var(--accent)}
.header-note{font-size:.8rem;color:var(--muted)}
.wrap{max-width:960px;margin:0 auto;padding:0 20px}
.hero{position:relative;overflow:hidden;margin-top:32px;padding:clamp(32px,6vw,64px);border:1px solid var(--line);border-radius:24px;background:linear-gradient(135deg,#eef2ff 0%,#fdf2f8 100%)}
.hero:before{content:"";position:absolute;right:-70px;top:-90px;width:300px;height:300px;border-radius:50%;background:radial-gradient(circle,rgba(59,91,219,.18),transparent 70%)}
.hero>*{position:relative}
.eyebrow{display:inline-block;font-size:.7rem;font-weight:800;letter-spacing:.16em;color:var(--accent);text-transform:uppercase}
.hero h1{font-size:clamp(2rem,5vw,3.1rem);line-height:1.12;letter-spacing:-.035em;margin:12px 0 14px}
.hero p{max-width:640px;margin:0 0 26px;color:var(--ink-soft);font-size:1.05rem}
.btn{display:inline-flex;align-items:center;gap:8px;background:var(--accent);color:#fff;padding:11px 19px;border-radius:999px;font-weight:650;font-size:.94rem;border:0}
.btn:hover{text-decoration:none;filter:brightness(1.06)}
.section{padding-top:52px}
.section-head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:20px}
.section-head h2{margin:0;font-size:1.5rem;letter-spacing:-.02em}
.section-sub{color:var(--muted);font-size:.92rem}
.page-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:18px}
.page-card{display:block;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:22px;box-shadow:var(--shadow);transition:transform .18s ease,border-color .18s ease}
.page-card:hover{transform:translateY(-3px);border-color:var(--accent);text-decoration:none}
.page-card h3{margin:7px 0 8px;font-size:1.08rem;line-height:1.4;color:var(--ink)}
.page-card p{margin:0;color:var(--muted);font-size:.92rem;line-height:1.6}
.card-eyebrow{font-size:.68rem;letter-spacing:.13em;text-transform:uppercase;color:var(--accent);font-weight:800}
.about p{max-width:720px;color:var(--ink-soft)}
.site-footer{border-top:1px solid var(--line);margin-top:72px;padding:28px 0 40px;color:var(--muted);font-size:.88rem}
@media(max-width:640px){.hero{margin-top:20px;padding:28px 22px}.section{padding-top:38px}.page-grid{grid-template-columns:1fr}}
@media(prefers-color-scheme:dark){:root{--bg:#0f1420;--card:#171d2b;--ink:#e8ecf5;--ink-soft:#c2cad9;--muted:#98a2b8;--line:#2a3346;--accent:#8aa4ff;--accent-soft:#1d2742;--shadow:none}.site-header{background:rgba(15,20,32,.88)}.hero{background:linear-gradient(135deg,#1a2240,#241b38)}}
"""


def _index_html(site: SiteConfig, description: str = "") -> str:
    esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    desc = esc(description or f"{site.brand} — practical guides from the Toolara toolbox.")
    return f"""<!DOCTYPE html>
<html lang="{site.lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(site.brand)} — Guides &amp; answers</title>
<meta name="description" content="{desc}">
<meta name="theme-color" content="#3b5bdb">
<link rel="stylesheet" href="/assets/site.css">
<link rel="canonical" href="https://{site.host}/">
</head>
<body>
<header class="site-header"><div class="header-inner">
  <a href="/" class="logo">{esc(site.brand)}<span class="logo-dot">.</span></a>
  <span class="header-note">Part of Toolara</span>
</div></header>
<main class="wrap">
  <section class="hero">
    <span class="eyebrow">Toolara guide</span>
    <h1>{esc(site.brand)}</h1>
    <p>{desc}</p>
    <a class="btn" href="#guides">Browse guides &darr;</a>
  </section>
  <section class="section" id="guides">
    <div class="section-head"><h2>Latest guides</h2>
      <span class="section-sub">Clear, practical answers &mdash; no fluff.</span></div>
    <div class="page-grid">
{PAGES_START}
      <article class="page-card"><span class="card-eyebrow">Coming soon</span>
        <h3>Guides are being written</h3>
        <p>Published pages will appear here automatically.</p></article>
{PAGES_END}
    </div>
  </section>
  <section class="section about">
    <div class="section-head"><h2>About {esc(site.brand)}</h2></div>
    <p>{esc(site.brand)} is part of the Toolara toolbox &mdash; a collection of free,
    focused pages that explain one topic well. Each guide is written for readers who
    want a straight answer without the noise.</p>
  </section>
</main>
<footer class="site-footer"><div class="wrap">
  {esc(site.brand)} &middot; Part of the <a href="https://{FLEET_DOMAIN}">Toolara toolbox</a>
</div></footer>
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
        public / "index.html": _index_html(site, description),
        public / "assets" / "site.css": SITE_CSS,
        public / "sitemap.xml": _sitemap_xml(site),
    }
    for path, text in files.items():
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(str(path.relative_to(work)))
    return written


def _git(work: Path, *args: str) -> None:
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


def _clone(work: Path, repo_full: str, branch: str) -> bool:
    """clone 远端到 work；失败（典型是空仓）返回 False，调用方走 init_and_push 首推。"""
    token = env("GITHUB_TOKEN", required=True)
    url = f"https://x-access-token:{token}@github.com/{repo_full}.git"
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch, url, str(work)],
        capture_output=True, text=True, timeout=300)
    return proc.returncode == 0


def init_and_push(work: Path, repo_full: str, branch: str) -> str:
    """空仓/新工作副本的首次提交推送（git 子进程只在 publishing.github_repo 之外的这里出现一次）。"""
    token = env("GITHUB_TOKEN", required=True)
    url = f"https://x-access-token:{token}@github.com/{repo_full}.git"
    _git(work, "init", "-b", branch)
    _git(work, "remote", "add", "origin", url)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init: scaffold static site")
    _git(work, "push", "-u", "origin", branch)
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
    elif _clone(work, site.repo, site.branch):
        # 本地没有副本但远端已有提交（容器重启后重跑）：clone 下来只补缺失文件再推
        scaffold = write_scaffold(work, site, description)
        if scaffold:
            _git(work, "add", "-A")
            _git(work, "commit", "-m", "init: scaffold static site")
            _git(work, "push", "origin", site.branch)
    else:
        # 真空仓：本地起 init 首推
        work.parent.mkdir(parents=True, exist_ok=True)
        work.mkdir(exist_ok=True)
        scaffold = write_scaffold(work, site, description)
        init_and_push(work, site.repo, site.branch)
    return {"ok": True, "repo": created["full_name"], "url": created["html_url"],
            "created": created["created"], "scaffold": scaffold}
