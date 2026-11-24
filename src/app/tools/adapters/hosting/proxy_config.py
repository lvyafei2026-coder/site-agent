# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""双仓配置接线：新站注册进 tool-proxy（转发配置）+ site-agent（站群清单真源）。

tool-proxy  改 src/sites.js（DOMAIN_MAP + SITE_PATHS）与 wrangler.toml（[[services]]），
            push 后由 tool-proxy 的 GitHub Actions（deploy.yml）自动 wrangler deploy。
site-agent  改 config/sites.json（站群清单唯一真源），push 后 Render 自动重启加载。

不动的：LAST_MOD（规则 2：绝不为登记而填日期，缺省比乱填好）、NOINDEX_PATHS、
LEGACY_PATH_MAP、HOSTS_WITH_OWN_FAVICON/OG（新站回落主站共享图）、KNOWN_HOSTS（派生）。
全部幂等：host 已登记则该处跳过。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from src.app.config import REPO_DIR, SiteConfig, env
from src.app.tools.adapters.publishing.github_repo import ensure_repo


def binding_name(repo_name: str) -> str:
    """repo → service binding 名：非 [A-Z0-9] 字符折叠成下划线（clocks-go-back → CLOCKS_GO_BACK）。"""
    return "SITE_" + re.sub(r"[^A-Z0-9]+", "_", repo_name.upper()).strip("_")


def _insert_object_entry(text: str, export_line: str, entry: str) -> str:
    """在 `export const X = { ... };` 对象体开头插入一条；对象体内已有该 host 则幂等跳过。

    幂等检查只看本对象体（split 到最近的 `};`），不看全文——否则 DOMAIN_MAP 插过
    host 后 SITE_PATHS 会被误判"已登记"而漏插。
    """
    host = entry.split("'")[1]
    anchor = export_line + "\n"
    if anchor not in text:
        raise RuntimeError(f"sites.js 里找不到锚点行：{export_line}")
    head, rest = text.split(anchor, 1)
    body, sep, tail = rest.partition("};")
    if host in body:
        return text
    return head + anchor + entry + "\n" + body + sep + tail


def patch_sites_js(text: str, site: SiteConfig, paths: list[str] | None = None) -> str:
    """DOMAIN_MAP + SITE_PATHS 两条插入（对象开头插入不破坏既有条目，顺序无关）。"""
    domain_entry = (f"  '{site.host}':"
                    f" {{ binding: '{binding_name(site.repo_name)}',"
                    f" service: '{site.repo_name}' }},")
    text = _insert_object_entry(text, "export const DOMAIN_MAP = {", domain_entry)
    paths_entry = f"  '{site.host}': {json.dumps(paths or ['/'])},"
    return _insert_object_entry(text, "export const SITE_PATHS = {", paths_entry)


def patch_wrangler_toml(text: str, site: SiteConfig) -> str:
    """追加一个 [[services]] binding（已有同名 binding 则幂等跳过）。"""
    binding = binding_name(site.repo_name)
    if f'binding = "{binding}"' in text:
        return text
    return (text.rstrip("\n")
            + f'\n\n[[services]]\nbinding = "{binding}"\nservice = "{site.repo_name}"\n')


def add_to_sites_json(text: str, site: SiteConfig) -> str:
    """站群清单追加新站（同名站点已存在则幂等跳过）。"""
    data = json.loads(text)
    if any(s.get("name") == site.name for s in data["sites"]):
        return text
    data["sites"].append({
        "name": site.name, "subdomain": site.subdomain,
        "repo": site.repo_name, "brand": site.brand, "lang": site.lang,
    })
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _js_syntax_ok(text: str) -> bool:
    """本地有 node 就 node --check 校验（Render 无 node 时退化为括号平衡检查）。"""
    tmp = Path(REPO_DIR).parent / "_sites_check.mjs"
    try:
        tmp.write_text(text, encoding="utf-8")
        proc = subprocess.run(["node", "--check", str(tmp)],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return text.count("{") == text.count("}") and text.count("(") == text.count(")")
    finally:
        tmp.unlink(missing_ok=True)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def _repo_copy(repo_full: str, branch: str = "main") -> Path:
    """clone-or-update 一份远端仓库工作副本（配置真源永远以远端为准）。"""
    class _RepoSite:
        """ensure_repo 只读 repo_name / repo / branch 三个字段，就地给一个。

        ★赋值必须走 __init__：类体是 `branch = branch` 的话，类体不闭包、读不到外层参数。
        """
        def __init__(self, full: str, br: str) -> None:
            self.name = "config-repo"
            self.repo_name = full.split("/", 1)[1]
            self.branch = br
            self.repo = full

    return ensure_repo(_RepoSite(repo_full, branch))  # type: ignore[arg-type]


def _commit_push(work: Path, message: str, branch: str) -> dict[str, Any]:
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                            cwd=str(work)).returncode != 0
    if staged:
        _git(work, "commit", "-m", message)
        _git(work, "push", "origin", branch)
        return {"pushed": True, "sha": _git(work, "rev-parse", "HEAD")}
    return {"pushed": False, "detail": "配置无变化，跳过提交"}


def register_in_proxy(site: SiteConfig, paths: list[str] | None = None) -> dict[str, Any]:
    """新站接线：tool-proxy（sites.js + wrangler.toml → push 触发其 CI 部署）+
    site-agent（config/sites.json → push 触发 Render 重启）。幂等。"""
    gh_org = env("GH_ORG", required=True)
    # tool-proxy：转发配置 + service binding
    proxy_work = _repo_copy(env("PROXY_REPO", f"{gh_org}/tool-proxy"))
    js_path = proxy_work / "src" / "sites.js"
    toml_path = proxy_work / "wrangler.toml"
    js_old = js_path.read_text(encoding="utf-8")
    js_new = patch_sites_js(js_old, site, paths)
    if js_new != js_old:
        if not _js_syntax_ok(js_new):
            raise RuntimeError("sites.js 改写后语法校验不过，拒绝提交")
        js_path.write_text(js_new, encoding="utf-8")
        toml_path.write_text(
            patch_wrangler_toml(toml_path.read_text(encoding="utf-8"), site),
            encoding="utf-8")
        _git(proxy_work, "add", "-A")
    proxy_result = {"changed": js_new != js_old,
                    **_commit_push(proxy_work, f"register site {site.name}", "main")}

    # site-agent：站群清单真源（push → Render 自动重启加载新 SITES）
    self_work = _repo_copy(env("SITE_AGENT_REPO", f"{gh_org}/site-agent"))
    sites_path = self_work / "config" / "sites.json"
    sites_old = sites_path.read_text(encoding="utf-8")
    sites_new = add_to_sites_json(sites_old, site)
    self_result: dict[str, Any] = {"changed": False}
    if sites_new != sites_old:
        sites_path.write_text(sites_new, encoding="utf-8")
        _git(self_work, "add", "-A")
        self_result = {"changed": True, **_commit_push(
            self_work, f"register site {site.name} in fleet config", "main")}

    return {"ok": True, "proxy": proxy_result, "self": self_result,
            "binding": binding_name(site.repo_name), "host": site.host}
