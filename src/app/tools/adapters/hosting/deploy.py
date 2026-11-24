# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Cloudflare 侧上线三步：建 Worker → Workers Builds 连仓 → 绑自定义域。

全部幂等：Worker 已存在 / 构建已连接 / 域名已绑定都按"复用"返回，不重复创建。
token 分工（Workers Builds API 必须 user-scoped，账户级 token 会 Invalid token）：
  CF_API_TOKEN     建 Worker、查 zone、绑自定义域（账户级即可）
  CF_BUILDS_TOKEN  Builds 连仓与 build token（user-scoped，未配置则 fail-fast）
一次性人工前提：Cloudflare GitHub App 已授权（dashboard 任意 Worker → Settings →
Builds → Connect；新仓库需在 App 的仓库授权范围内）。
"""

from __future__ import annotations

from typing import Any

from src.app.config import SiteConfig, env
from src.app.tools.adapters.cloudflare.client import CloudflareClient
from src.app.tools.adapters.hosting.site_scaffold import repo_info

workers = CloudflareClient()                                  # CF_API_TOKEN
builds = CloudflareClient(api_token=env("CF_BUILDS_TOKEN", ""))


def _builds_token() -> str:
    if not builds._api_token:
        raise RuntimeError("未配置 CF_BUILDS_TOKEN（user-scoped，Workers Builds API 专用）；"
                           "账户级 CF_API_TOKEN 过不了 Builds API。")
    return builds._api_token


def _token_uuid() -> str:
    result = builds.result("GET", "builds", "tokens")
    for row in result:
        if row.get("build_token_uuid"):
            return row["build_token_uuid"]
    raise RuntimeError("账户里没有可用的 build token（dashboard → Worker Settings → Builds → API token）")


def worker_exists(name: str) -> bool:
    scripts = workers.result("GET", "workers", "scripts")
    if any(s.get("id") == name for s in scripts):
        return True
    # 空壳 Worker（还没被 Builds 部署过脚本）不出现在 scripts 列表里，单独探一次
    probe = workers.request("GET", "workers", "workers", name)
    return probe.status_code == 200


def create_worker(name: str) -> dict[str, Any]:
    """幂等建 Worker（空壳，内容由 Workers Builds 从仓库部署）。返回 {name, tag}。"""
    if worker_exists(name):
        return {"ok": True, "created": False, "name": name, "tag": _worker_tag(name)}
    result = workers.result("POST", "workers", "workers", json={"name": name})
    return {"ok": True, "created": True, "name": name, "tag": result.get("tag", "")}


def _worker_tag(name: str) -> str:
    scripts = workers.result("GET", "workers", "scripts")
    for s in scripts:
        if s.get("id") == name:
            tag = s.get("tag", "")
            if tag:
                return tag
    # 空壳 Worker 不在 scripts 列表里：GET workers/workers/{name} 的 id 就是 script tag
    info = workers.result("GET", "workers", "workers", name)
    tag = info.get("id", "")
    if tag:
        return tag
    raise RuntimeError(f"Worker {name} 拿不到 tag（Workers Scripts Read）")


def connect_build(site: SiteConfig) -> dict[str, Any]:
    """幂等连仓：Worker + Workers Builds 接管 GitHub 仓库（push 即 wrangler deploy 自动部署）。

    repo_id 是 GitHub 数字 id（site_scaffold.repo_info 提供），Builds API 不认仓库名字符串。
    """
    _builds_token()
    worker = create_worker(site.repo_name)
    tag = worker["tag"]
    existing = builds.request("GET", "builds", "workers", tag)
    if existing.status_code == 200:
        body = existing.json().get("result") or {}
        if (body.get("git_repository") or {}).get("repo_name"):
            return {"ok": True, "created": False, "worker": site.repo_name,
                    "tag": tag, "repo": body["git_repository"].get("repo_name")}
    info = repo_info(site.repo)
    if not info:
        raise RuntimeError(f"仓库 {site.repo} 不存在或 GITHUB_TOKEN 看不到（先跑建站仓库步骤）")
    builds.result("POST", "builds", "workers", json={
        "script_tag": tag,
        "git_repository": {
            # Builds API 这些 id 字段只收字符串，传 GitHub 的 int 会 12002 Invalid request body
            "provider_type": "github",
            "provider_account_id": str(info["owner"]["id"]),
            "provider_account_name": info["owner"]["login"],
            "repo_id": str(info["id"]), "repo_name": info["name"], "branch": site.branch,
        },
        "production_settings": {
            "build_command": "npm run build",
            "deploy_command": "npx wrangler deploy",
            "build_token_uuid": _token_uuid(),
        },
        "previews_enabled": False,
        # ★previews_base_config 是必填（官方文档正文没写明），缺了整包 12002 Invalid request body
        "previews_base_config": {
            "build_command": "npm run build",
            "deploy_command": "npx wrangler preview",
            "build_token_uuid": _token_uuid(),
        },
    })
    return {"ok": True, "created": True, "worker": site.repo_name,
            "tag": tag, "repo": info["full_name"]}


def _zone_id() -> str:
    # /zones 是账户级之外的端点（不带 /accounts/{id} 前缀）
    result = workers.result("GET", "zones", params={"name": env("FLEET_DOMAIN", "toolara.dev")},
                            account_level=False)
    for z in result:
        if z.get("id"):
            return z["id"]
    raise RuntimeError(f"账户里找不到 zone {env('FLEET_DOMAIN', 'toolara.dev')}")


def _bound_domains() -> list[dict]:
    return workers.result("GET", "workers", "domains") or []


def attach_domain(site: SiteConfig) -> dict[str, Any]:
    """幂等绑自定义域：hostname 必须带 zone（FLEET_DOMAIN 的子域），DNS 记录自动创建。"""
    hostname = site.host
    for d in _bound_domains():
        if d.get("hostname") == hostname:
            return {"ok": True, "created": False, "hostname": hostname,
                    "service": d.get("service")}
    workers.result("PUT", "workers", "domains", json={
        "zone_id": _zone_id(), "hostname": hostname,
        "service": site.repo_name, "environment": "production",
    })
    return {"ok": True, "created": True, "hostname": hostname,
            "service": site.repo_name}
