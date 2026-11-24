# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""hosting 域（新站上线四步）+ launch_new_site 工具。

外部 IO 全部打桩：GitHub REST / git 子进程 / Cloudflare REST 都 monkeypatch 掉，
只验证幂等分支、补丁文本正确性与编排顺序——真实凭据的连通性属于运维演练，不进单测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.app.config import SiteConfig, load_sites
from src.app.tools.adapters.hosting import proxy_config, site_scaffold
from src.app.tools.adapters.hosting import deploy as cf_deploy
from src.app.tools.hosting_tools import SiteSetupOps

NEW_SITE = SiteConfig(name="freshsite", repo_name="fresh-site-tool",
                      subdomain="freshsite", brand="Fresh Site Tool")


@pytest.fixture(autouse=True)
def _gh_org(monkeypatch):
    monkeypatch.setenv("GH_ORG", "acme")


# ---- 补丁文本（纯函数）----

def test_patch_sites_js_inserts_domain_and_paths():
    sample = ("export const DOMAIN_MAP = {\n"
              "  'music.toolara.dev': { binding: 'SITE_MUSIC', service: 'ai-music-generator' }\n"
              "};\n\n"
              "export const SITE_PATHS = {\n"
              "  'music.toolara.dev': [\"/\"]\n"
              "};\n")
    out = proxy_config.patch_sites_js(sample, NEW_SITE)
    assert "'freshsite.toolara.dev': { binding: 'SITE_FRESH_SITE_TOOL'," in out
    assert "service: 'fresh-site-tool' }" in out
    assert "'freshsite.toolara.dev': [\"/\"]," in out
    assert out.count("freshsite.toolara.dev") == 2     # DOMAIN_MAP + SITE_PATHS 各一条


def test_patch_sites_js_idempotent_and_braces_balanced():
    sample = "export const DOMAIN_MAP = {\n};\nexport const SITE_PATHS = {\n};\n"
    once = proxy_config.patch_sites_js(sample, NEW_SITE)
    twice = proxy_config.patch_sites_js(once, NEW_SITE)
    assert twice == once
    assert once.count("{") == once.count("}")


def test_binding_name_folds_non_alnum():
    assert proxy_config.binding_name("clocks-go-back") == "SITE_CLOCKS_GO_BACK"
    assert proxy_config.binding_name("code-tools") == "SITE_CODE_TOOLS"


def test_patch_wrangler_toml_appends_service(monkeypatch):
    monkeypatch.setattr(proxy_config, "_js_syntax_ok", lambda _t: True)
    toml = ('name = "tool-proxy"\n\n[[services]]\nbinding = "SITE_MUSIC"\n'
            'service = "ai-music-generator"\n')
    out = proxy_config.patch_wrangler_toml(toml, NEW_SITE)
    assert 'binding = "SITE_FRESH_SITE_TOOL"' in out
    assert 'service = "fresh-site-tool"' in out
    assert proxy_config.patch_wrangler_toml(out, NEW_SITE) == out   # 幂等


def test_add_to_sites_json_appends_and_idempotent(tmp_path):
    raw = json.dumps({"_comment": "x", "sites": [{"name": "music"}]}, indent=2)
    out = proxy_config.add_to_sites_json(raw, NEW_SITE)
    data = json.loads(out)
    assert {"name": "freshsite", "subdomain": "freshsite",
            "repo": "fresh-site-tool",
            "brand": "Fresh Site Tool", "lang": "en"} in data["sites"]
    assert "_comment" in data
    assert proxy_config.add_to_sites_json(out, NEW_SITE) == out


# ---- GitHub 建仓（REST 打桩）----

def test_create_site_repo_scaffold_and_push(monkeypatch, tmp_path):
    monkeypatch.setattr(site_scaffold, "REPO_DIR", tmp_path / "repos")
    monkeypatch.setattr(site_scaffold, "repo_info", lambda full: None)
    monkeypatch.setattr(site_scaffold, "github_rest",
                        lambda method, path, payload=None: {"status": 201, "body": {
                            "id": 5, "full_name": f"acme/{payload['name']}",
                            "html_url": f"https://github.com/acme/{payload['name']}"}})
    pushed = {}
    monkeypatch.setattr(site_scaffold, "init_and_push",
                        lambda work, repo_full, branch: pushed.update(
                            repo=repo_full, branch=branch, work=str(work)) or "abc123")

    out = site_scaffold.create_site_repo(NEW_SITE, "calculator")

    assert out["ok"] is True and out["created"] is True and out["repo"] == "acme/fresh-site-tool"
    assert pushed == {"repo": "acme/fresh-site-tool", "branch": "main",
                      "work": str(tmp_path / "repos" / "fresh-site-tool")}
    work = Path(pushed["work"])
    assert (work / "wrangler.toml").read_text().startswith('name = "fresh-site-tool"')
    assert "[assets]" in (work / "wrangler.toml").read_text()
    assert json.loads((work / "package.json").read_text())["scripts"]["deploy"] == "wrangler deploy"
    assert "<h1>" in (work / "public" / "index.html").read_text()
    assert "freshsite.toolara.dev" in (work / "public" / "sitemap.xml").read_text()


def test_create_site_repo_existing_repo_and_existing_files(monkeypatch, tmp_path):
    """仓库已存在 + 本地文件齐全 → 幂等：不重建、不覆盖、不推送。"""
    monkeypatch.setattr(site_scaffold, "REPO_DIR", tmp_path / "repos")
    monkeypatch.setattr(site_scaffold, "repo_info",
                        lambda full: {"id": 99, "full_name": full, "html_url": f"https://github.com/{full}"})
    work = tmp_path / "repos" / "fresh-site-tool"
    work.mkdir(parents=True)
    (work / ".git").mkdir()
    (work / "wrangler.toml").write_text("name = \"mine\"")
    (work / "public").mkdir()
    (work / "public" / "index.html").write_text("keep me")
    called = {"push": False}
    monkeypatch.setattr(site_scaffold, "init_and_push", lambda *a: called.update(push=True))

    out = site_scaffold.create_site_repo(NEW_SITE, "calculator")

    assert out == {"ok": True, "repo": "acme/fresh-site-tool",
                   "url": "https://github.com/acme/fresh-site-tool",
                   "created": False,
                   "scaffold": ["package.json", str(Path("public") / "sitemap.xml")]}
    assert called["push"] is False
    assert (work / "wrangler.toml").read_text() == "name = \"mine\""   # 已有文件不覆盖
    assert (work / "public" / "index.html").read_text() == "keep me"
    assert json.loads((work / "package.json").read_text())["name"] == "fresh-site-tool"


# ---- Cloudflare 三步（REST 打桩）----

class FakeCF:
    """CloudflareClient.request 替身：签名与真 request 对齐（method, *segments），
    按 (method, url) 回预设响应并记录调用。"""

    account_id = "acct"

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, *segments, params=None, json=None, data=None,
                 timeout=30, account_level=True):
        base = "https://api.cloudflare.com/client/v4"
        path = "/".join(str(s) for s in segments)
        url = f"{base}/{path}" if account_level else f"{base}/accounts/{self.account_id}/{path}"
        url = f"{base}/accounts/{self.account_id}/{path}" if account_level else f"{base}/{path}"
        self.calls.append((method, url, json))
        handler = self.routes.get((method, url))
        if callable(handler):
            return handler(method, url, json)
        import types
        resp = types.SimpleNamespace(status_code=200,
                                     json=lambda: {"success": True, "result": handler})
        return resp


@pytest.fixture
def site(monkeypatch):
    s = SiteConfig(name="freshsite", repo_name="fresh-site-tool",
                   subdomain="freshsite", brand="Fresh Site Tool")
    monkeypatch.setattr(cf_deploy.workers, "_account_id", "acct")
    monkeypatch.setattr(cf_deploy.workers, "_api_token", "tok")
    monkeypatch.setattr(cf_deploy.builds, "_api_token", "usertok")
    return s


def test_connect_build_full_flow(site, monkeypatch):
    monkeypatch.setattr(cf_deploy.workers, "request", FakeCF({
        ("GET", "https://api.cloudflare.com/client/v4/accounts/acct/workers/scripts"):
            [{"id": "fresh-site-tool", "tag": "tag1"}],
        ("GET", "https://api.cloudflare.com/client/v4/accounts/acct/builds/tokens"):
            [{"build_token_uuid": "bt-1"}],
        ("POST", "https://api.cloudflare.com/client/v4/accounts/acct/workers/workers"):
            {"id": "fresh-site-tool", "tag": "tag1"},
        ("POST", "https://api.cloudflare.com/client/v4/accounts/acct/builds/workers"):
            [{"script_tag": "tag1"}],
    }).__call__)
    builds_fake = FakeCF({
        ("GET", "https://api.cloudflare.com/client/v4/accounts/acct/builds/workers/tag1"):
            None,   # 404-ish：无连接
        ("GET", "https://api.cloudflare.com/client/v4/accounts/acct/builds/tokens"):
            [{"build_token_uuid": "bt-1"}],
        ("POST", "https://api.cloudflare.com/client/v4/accounts/acct/builds/workers"):
            {"script_tag": "tag1"},
    })
    monkeypatch.setattr(cf_deploy.builds, "request", builds_fake.__call__)
    monkeypatch.setattr(cf_deploy, "repo_info",
                        lambda full: {"id": 7, "full_name": full,
                                      "owner": {"id": 1, "login": "acme"}, "name": "fresh-site-tool"})

    out = cf_deploy.connect_build(site)

    assert out["ok"] is True and out["created"] is True and out["tag"] == "tag1"
    post = next(c for c in builds_fake.calls if c[0] == "POST" and "builds/workers" in c[1])
    body = post[2]
    assert body["script_tag"] == "tag1" and body["git_repository"]["repo_id"] == 7
    assert body["production_settings"]["deploy_command"] == "npx wrangler deploy"
    assert body["production_settings"]["build_token_uuid"] == "bt-1"


def test_connect_build_already_connected(site, monkeypatch):
    import types
    monkeypatch.setattr(cf_deploy.workers, "request", FakeCF({
        ("GET", "https://api.cloudflare.com/client/v4/accounts/acct/workers/scripts"):
            [{"id": "fresh-site-tool", "tag": "tag1"}],
    }).__call__)

    def fake_builds_get(method, *segments, **kw):
        return types.SimpleNamespace(status_code=200, json=lambda: {
            "success": True, "result": {"git_repository": {"repo_name": "fresh-site-tool"}}})

    monkeypatch.setattr(cf_deploy.builds, "request", fake_builds_get)
    out = cf_deploy.connect_build(site)
    assert out == {"ok": True, "created": False, "worker": "fresh-site-tool",
                   "tag": "tag1", "repo": "fresh-site-tool"}


def test_connect_build_without_user_token_fails_fast(site, monkeypatch):
    monkeypatch.setattr(cf_deploy.builds, "_api_token", "")
    with pytest.raises(RuntimeError, match="CF_BUILDS_TOKEN"):
        cf_deploy.connect_build(site)


def test_attach_domain_creates_and_idempotent(site, monkeypatch):
    calls = FakeCF({
        ("GET", "https://api.cloudflare.com/client/v4/accounts/acct/workers/domains"):
            [{"hostname": "music.toolara.dev", "service": "ai-music-generator"}],
        ("GET", "https://api.cloudflare.com/client/v4/zones"):
            [{"id": "zone1", "name": "toolara.dev"}],
        ("PUT", "https://api.cloudflare.com/client/v4/accounts/acct/workers/domains"):
            [{"hostname": "freshsite.toolara.dev"}],
    })
    monkeypatch.setattr(cf_deploy.workers, "request", calls.__call__)

    out = cf_deploy.attach_domain(site)

    assert out == {"ok": True, "created": True, "hostname": "freshsite.toolara.dev",
                   "service": "fresh-site-tool"}
    put = next(c for c in calls.calls if c[0] == "PUT")
    assert put[2]["zone_id"] == "zone1" and put[2]["service"] == "fresh-site-tool"

    # 已绑定 → 幂等复用
    calls.routes[("GET", "https://api.cloudflare.com/client/v4/accounts/acct/workers/domains")] = \
        [{"hostname": "freshsite.toolara.dev", "service": "fresh-site-tool"}]
    assert cf_deploy.attach_domain(site)["created"] is False


# ---- 编排（四步全打桩）----

def test_launch_new_site_chains_four_steps(monkeypatch):
    ops = SiteSetupOps()
    seen = []

    def stub(fn_name, result):
        def call(site, *a, **kw):
            seen.append((fn_name, site.name))
            return result
        return call

    monkeypatch.setattr("src.app.tools.hosting_tools.create_site_repo",
                        stub("repo", {"ok": True, "repo": "acme/fresh-site-tool", "url": "u", "created": True}))
    monkeypatch.setattr("src.app.tools.hosting_tools.connect_build",
                        stub("build", {"ok": True, "tag": "t1"}))
    monkeypatch.setattr("src.app.tools.hosting_tools.attach_domain",
                        stub("domain", {"ok": True, "hostname": "freshsite.toolara.dev"}))
    monkeypatch.setattr("src.app.tools.hosting_tools.register_in_proxy",
                        stub("proxy", {"ok": True, "binding": "SITE_FRESHSITE", "host": "freshsite.toolara.dev"}))

    out = ops.launch_new_site("freshsite", brand="Fresh Site Tool")

    assert out["ok"] is True and out["binding"] == "SITE_FRESHSITE"
    assert [s for s, _ in seen] == ["repo", "build", "domain", "proxy"]
    assert all(v.get("ok") for v in out["steps"].values())


def test_launch_new_site_stops_on_first_failure(monkeypatch):
    ops = SiteSetupOps()
    monkeypatch.setattr("src.app.tools.hosting_tools.create_site_repo",
                        lambda site, *a, **kw: {"ok": True})
    monkeypatch.setattr("src.app.tools.hosting_tools.connect_build",
                        lambda site, *a, **kw: (_ for _ in ()).throw(RuntimeError("CF 401")))
    stopped = {"after": []}
    monkeypatch.setattr("src.app.tools.hosting_tools.attach_domain",
                        lambda site, *a, **kw: stopped["after"].append("domain") or {"ok": True})

    out = ops.launch_new_site("freshsite")

    assert out["ok"] is False and "build" in out["error"]
    assert stopped["after"] == []                 # 失败即停，后续步骤不跑


def test_launch_new_site_rejects_known_site():
    ops = SiteSetupOps()
    out = ops.launch_new_site("music")
    assert out["ok"] is False and "已在站群清单" in out["error"]
    out = ops.launch_new_site("  ")
    assert out["ok"] is False and "站点名" in out["error"]


def test_registry_has_launch_new_site():
    """31 号工具进注册表，kind=write（不可逆动作显式声明）。"""
    from src.app.tools.registry import tool_registry
    tool = tool_registry.get("launch_new_site")
    assert tool.kind == "write"
    assert "launch_new_site" in tool_registry.names()


def test_fleet_still_loads(tmp_path, monkeypatch):
    """config/sites.json 的加载契约不被 hosting 改动破坏（新增站点只改 JSON）。"""
    path = tmp_path / "sites.json"
    path.write_text(json.dumps({"sites": [
        {"name": "a"}, {"name": "b", "repo": "b-repo", "brand": "B"},
    ]}), encoding="utf-8")
    sites = load_sites(path)
    assert sites["b"].repo_name == "b-repo" and sites["b"].brand == "B"
    assert sites["a"].subdomain == "a"
