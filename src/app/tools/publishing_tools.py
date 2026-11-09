# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""publishing 域：内容页台账、渲染、推送、构建验活（定义 + 实现同文件）。"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field

from src.app.config import SITES
from src.app.tools.adapters import hosting
from src.app.tools.adapters.cloudflare import database
from src.app.tools.adapters.cloudflare.sql import PAGE_QUERIES
from src.app.tools.base import RuntimeTool, stable_key


class PublishingOps:
    """publishing 域的实现：D1 台账 + hosting 渲染/git/验活。

    跨域复用直接写 self.<method>（门面组合后可见，如 queue_indexing 落 ledger 事件），
    不额外 import 其它域的 Ops——换实现只换门面，一处生效。
    """

    def page_inventory(self, mode="all", limit=200):
        query = PAGE_QUERIES.get(mode) or PAGE_QUERIES["all"]
        params = ([int(time.time()) - query.window_days * 86400]
                  if query.window_days else [])
        return database.query(f"{query.sql} LIMIT ?", [*params, limit])

    def build_status(self, repo, sha):
        return hosting.check_runs(repo, sha)

    def live_page(self, site_name, slug, expect=""):
        site = SITES.get(site_name)
        if not site:
            return {"ok": False, "detail": f"未知站点 {site_name}"}
        ok, detail = hosting.verify_live(site, slug, expect)
        return {"ok": ok, "url": detail, "detail": detail}

    def render_files(self, site_name, page):
        return hosting.render_files(SITES[site_name], page)

    def git_push(self, site_name, page, work):
        return hosting.push_files(SITES[site_name], page, work)

    def wait_build(self, repo, sha, work="", branch="main"):
        return hosting.wait_build(repo, sha, work=work, branch=branch)

    def queue_indexing(self, url):
        # 普通内容页 Indexing API 不生效；sitemap 已在 render_files 阶段写入。
        return self.ledger_upsert("agent_events", {
            "event_key": stable_key("indexing", url), "kind": "indexing-queued",
            "payload": json.dumps({"url": url}), "status": "pending",
            "created_at": int(time.time()),
        })


class GetPageInventoryTool(RuntimeTool):
    """读取已建内容页台账。mode=all 查重 / recent 最近上线 / new 近 30 天新页 / review 上线满 7 天待复盘。"""

    tool_name = "get_page_inventory"
    method = "page_inventory"

    class Input(BaseModel):
        mode: str = Field('all', description="all|recent|new|review")
        limit: int = Field(200, description="最多返回几条")


class GetBuildStatusTool(RuntimeTool):
    """查询某次 git commit 在 GitHub 上的 check run（Cloudflare Workers Builds）。"""

    tool_name = "get_build_status"
    method = "build_status"

    class Input(BaseModel):
        repo: str = Field(..., description="owner/repo")
        sha: str = Field(..., description="commit sha")


class CheckLivePageTool(RuntimeTool):
    """带随机参数穿透缓存，验证线上页面返回 200 且含预期标题。"""

    tool_name = "check_live_page"
    method = "live_page"

    class Input(BaseModel):
        site_name: str = Field(..., description="SITES 里的站点名")
        slug: str = Field(..., description="页面 slug")
        expect: str = Field('', description="预期标题片段")


class RenderFilesTool(RuntimeTool):
    """把 LLM 生成的页面渲染成静态 HTML 并更新站点 sitemap（本地文件，可逆）。"""

    tool_name = "render_files"
    method = "render_files"
    kind = "ledger"

    class Input(BaseModel):
        site_name: str = Field(..., description="SITES 里的站点名")
        page: dict[str, Any] = Field(..., description="页面结构")


class WaitBuildTool(RuntimeTool):
    """等待 GitHub 上的 Workers Builds check run 完成，失败时做一次空提交重推。"""

    tool_name = "wait_build"
    method = "wait_build"
    kind = "ledger"

    class Input(BaseModel):
        repo: str = Field(..., description="owner/repo")
        sha: str = Field(..., description="commit sha")
        work: str = Field('', description="本地仓库路径，用于重推")
        branch: str = Field('main', description="分支，默认 main")


class QueueIndexingTool(RuntimeTool):
    """把新页面 URL 记入待收录清单。sitemap 已在 render_files 阶段写好，普通内容页不走 Indexing API。"""

    tool_name = "queue_indexing"
    method = "queue_indexing"
    kind = "ledger"

    class Input(BaseModel):
        url: str = Field(..., description="线上完整 URL")


class GitPushTool(RuntimeTool):
    """把已渲染的文件 commit 并 push 到子站仓库，触发 Cloudflare 自动构建。不可逆，必须人工批准。"""

    tool_name = "git_push"
    method = "git_push"
    kind = "write"

    class Input(BaseModel):
        site_name: str = Field(..., description="SITES 里的站点名")
        page: dict[str, Any] = Field(..., description="页面结构")
        work: str = Field(..., description="本地仓库路径，由 render_files 返回")
