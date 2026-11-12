# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""集中配置：Settings 单例（全部来自 .env）+ 站群清单 + 意图枚举。

所有模块只 import `settings` 或本文件的常量，不各自读 .env。
站群清单在 config/sites.json（SITES_FILE 可覆盖），域名只有 FLEET_DOMAIN 一处。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "templates"


def env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        raise RuntimeError(f"缺少必需环境变量：{key}")
    return value or ""


@dataclass(slots=True)
class Settings:
    """进程级开关，全部来自 .env。"""

    project_name: str = "site-agent"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    memory_backend: str = os.getenv("MEMORY_BACKEND", "memory").lower()
    database_url: str = os.getenv("DATABASE_URL", "")
    langsmith_tracing: bool = os.getenv("LANGSMITH_TRACING", "false").lower() == "true"
    langsmith_endpoint: str = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    langsmith_api_key: str = os.getenv("LANGSMITH_API_KEY", "")
    langsmith_project: str = os.getenv("LANGSMITH_PROJECT", "site-agent")
    # 项目页地址（不是凭据）：填了之后 /run 会带回去，console 的结果卡上就有直达链接
    langsmith_project_url: str = os.getenv("LANGSMITH_PROJECT_URL", "")
    default_prompt_versions: dict[str, str] | None = None
    max_validation_retries: int = int(env("MAX_VALIDATION_RETRIES", "2"))

    def __post_init__(self) -> None:
        if self.default_prompt_versions is None:
            self.default_prompt_versions = {"intent": "v1", "extraction": "v1"}

    @property
    def prompt_root(self) -> Path:
        return Path(__file__).resolve().parent / "prompts" / "versions"


settings = Settings()

# 一轮运营的意图枚举（唯一真源）：llm 负责分类、tool_service 负责映射工具。
# analyze_hotwords：批量分析候选池里「未跟进」的热词并回写结论状态（有副作用）。
INTENTS: tuple[str, ...] = (
    "select_pages", "plan_promo", "find_backlinks", "review_metrics",
    "analyze_hotwords", "general",
)

# ---- 站群唯一真源 ----
FLEET_DOMAIN = env("FLEET_DOMAIN", "toolara.dev")


@dataclass(frozen=True)
class SiteConfig:
    """一个子站的部署目标（repo 名 = Worker 名；目录名默认同 repo）。"""

    name: str
    repo_name: str
    subdomain: str = ""
    brand: str = ""
    lang: str = "en"
    branch: str = "main"
    daily_limit: int = 5
    asset_dir: str = "public"

    @property
    def host(self) -> str:
        return f"{self.subdomain}.{FLEET_DOMAIN}" if self.subdomain else FLEET_DOMAIN

    @property
    def repo(self) -> str:
        return f"{env('GH_ORG', required=True)}/{self.repo_name}"

    @property
    def url(self) -> str:
        return f"https://{self.host}"


SITES_FILE = Path(env("SITES_FILE", str(PROJECT_ROOT / "config" / "sites.json")))


def load_sites(path: Path = SITES_FILE) -> dict[str, SiteConfig]:
    """读 config/sites.json 得到站点清单：站点信息是配置，不是代码。

    JSON 里 name 必填（同时是子域名，写工具用它定位站点），repo 缺省同名；
    subdomain / brand / lang / branch / daily_limit / asset_dir 都有缺省值。
    asset_dir 是站点内容目录——renderer 写 <asset_dir>/<slug>.html 与
    <asset_dir>/sitemap.xml，必须与仓库 wrangler.toml 的 assets.directory 一致。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    sites = [
        SiteConfig(
            name=item["name"],
            repo_name=item.get("repo") or item["name"],
            subdomain=item.get("subdomain", item["name"]),
            brand=item.get("brand", ""),
            lang=item.get("lang", "en"),
            branch=item.get("branch", "main"),
            daily_limit=int(item.get("daily_limit", 5)),
            asset_dir=item.get("asset_dir") or "public",
        )
        for item in raw["sites"]
    ]
    names = [s.name for s in sites]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"{path} 里站点重名：{dupes}")
    return {s.name: s for s in sites}


# 写路径白名单 = 上面这份清单。只有 write/verify 工具用它把 site_name 映射到
# 「仓库 + host + 品牌 + 资产目录」：render_files / git_push / live_page。
# 读工具（hotwords / click_stats / page_inventory）的站点维度来自 D1 的 site 列
# 与 radar，不查这张表。能接入的前提：仓库是资产目录布局（wrangler.toml 的
# assets.directory 指向它）、页面能套 templates/site-page.html、有 Contents 写权限。
SITES: dict[str, SiteConfig] = load_sites()

RADAR_URL = env("RADAR_URL", f"https://radar.{FLEET_DOMAIN}")
# 整理侧读写：/api/hotwords（取未跟进候选 + 交回分析结论）。console 是 Basic Auth，
# 密码用 CONSOLE_PASS（与 console 的 CONSOLE_PASS secret 同值），用户名任意。
CONSOLE_HQR = env("CONSOLE_HQR", f"https://console.{FLEET_DOMAIN}")
CONSOLE_PASS = env("CONSOLE_PASS", "")
CONTACT_EMAIL = env("CONTACT_EMAIL", f"hello@{FLEET_DOMAIN}")

# 建站仓库的本地工作副本（git clone 落盘位置）
REPO_DIR = Path(env("AGENT_STATE_DIR", str(PROJECT_ROOT / "data"))) / "repos"
REPO_DIR.mkdir(parents=True, exist_ok=True)

# ---- LLM / 外部凭据 ----
LLM_API_KEY = env("LLM_API_KEY", "")
LLM_BASE_URL = env("LLM_BASE_URL", "")
LLM_MODEL = env("LLM_MODEL", "gpt-4o-mini")
