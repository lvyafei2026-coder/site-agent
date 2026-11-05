# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""内容页渲染 + git push 部署 + 线上验证。

部署链路：写文件 → commit → push → 轮询 CF 构建 check run → 线上 200 校验
（沿用站群经验：push 后必须查 check run；0 个则空提交重推；上线只认线上页面）
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from ..config import REPO_DIR, TEMPLATE_DIR, SiteConfig, env

log = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")


class DeployError(RuntimeError):
    pass


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise DeployError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def _auth_url(repo: str) -> str:
    token = env("GITHUB_TOKEN", required=True)
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def ensure_repo(site: SiteConfig) -> Path:
    """首次 clone，之后 pull。目录名 = 仓库名。"""
    work = REPO_DIR / site.repo_name
    if (work / ".git").exists():
        git(work, "fetch", "--depth", "1", "origin", site.branch)
        git(work, "reset", "--hard", f"origin/{site.branch}")
        return work
    work.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", site.branch,
         _auth_url(site.repo), str(work)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return work


def render_page(site: SiteConfig, page: dict[str, Any]) -> str:
    tpl = (TEMPLATE_DIR / "site-page.html").read_text(encoding="utf-8")

    def esc(text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    sections = []
    for sec in page.get("sections", []):
        paras = "".join(f"<p>{esc(p)}</p>" for p in sec.get("paragraphs", []))
        sections.append(f"<h2>{esc(sec.get('heading', ''))}</h2>{paras}")

    faq_html = "".join(
        f"<h3>{esc(item['q'])}</h3><p>{esc(item['a'])}</p>"
        for item in page.get("faq", [])
    )

    jsonld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Article",
                "headline": page["title"],
                "description": page["description"],
                "mainEntityOfPage": f"https://{site.host}/{page['slug']}.html",
            },
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": item["q"],
                        "acceptedAnswer": {"@type": "Answer", "text": item["a"]},
                    }
                    for item in page.get("faq", [])
                ],
            },
        ],
    }

    tokens = {
        "lang": site.lang,
        "brand": site.brand,
        "host": site.host,
        "slug": page["slug"],
        "title": esc(page["title"]),
        "description": esc(page["description"]),
        "h1": esc(page.get("h1") or page["title"]),
        "intro": esc(page.get("intro", "")),
        "sections": "\n".join(sections),
        "faq": faq_html,
        "jsonld": json.dumps(jsonld, ensure_ascii=False),
    }
    for key, value in tokens.items():
        tpl = tpl.replace("{{" + key + "}}", value)
    return tpl


def _write_page(work: Path, site: SiteConfig, page: dict[str, Any]) -> Path:
    public = work / "public"
    public.mkdir(exist_ok=True)
    target = public / f"{page['slug']}.html"
    target.write_text(render_page(site, page), encoding="utf-8")
    return target


def _update_sitemap(work: Path, site: SiteConfig, slug: str) -> None:
    sitemap = work / "public" / "sitemap.xml"
    if not sitemap.exists():
        return
    text = sitemap.read_text(encoding="utf-8")
    loc = f"<loc>https://{site.host}/{slug}.html</loc>"
    if loc in text:
        return
    entry = f"  <url>{loc}<changefreq>monthly</changefreq></url>\n"
    sitemap.write_text(text.replace("</urlset>", entry + "</urlset>"), encoding="utf-8")


def _head_sha(work: Path) -> str:
    return git(work, "rev-parse", "HEAD")


def _check_runs(repo: str, sha: str) -> list[dict[str, Any]]:
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


def wait_check_run(repo: str, sha: str, timeout_sec: int = 360) -> bool:
    """等待 CF 构建完成；期间允许 GitHub 延迟出现 check run。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        runs = [r for r in _check_runs(repo, sha) if "Workers Builds" in r.get("name", "")]
        if runs:
            if all(r.get("status") == "completed" for r in runs):
                return all(r.get("conclusion") == "success" for r in runs)
        time.sleep(15)
    return False


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


def deploy_page(site: SiteConfig, page: dict[str, Any]) -> dict[str, Any]:
    slug = page.get("slug", "")
    if not SLUG_RE.match(slug):
        raise DeployError(f"非法 slug: {slug!r}")

    work = ensure_repo(site)
    _write_page(work, site, page)
    _update_sitemap(work, site, slug)

    git(work, "add", "-A")
    # 无变更时不重复提交
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=work).returncode == 0:
        return {"ok": True, "slug": slug, "detail": "无变更，跳过提交"}

    git(work, "commit", "-m", f"add: {slug}")
    git(work, "push", "origin", site.branch)
    sha = _head_sha(work)

    if not wait_check_run(site.repo, sha):
        log.warning("构建未成功，空提交重推：%s", slug)
        git(work, "commit", "--allow-empty", "-m", "ci: retrigger")
        git(work, "push", "origin", site.branch)
        sha = _head_sha(work)
        if not wait_check_run(site.repo, sha):
            return {"ok": False, "slug": slug, "detail": "CF 构建失败（已重推一次）"}

    ok, detail = verify_live(site, slug, page.get("title", ""))
    return {"ok": ok, "slug": slug, "sha": sha, "detail": detail}


# 保留：CI 需要 token 时必须显式注入，避免 git 交互式提示
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
