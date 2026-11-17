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
            # ★D1 读不到默认版本时的兜底版本号（不是提示词正文）。唯一真源在 radar-rules
            #   库的 prompt_defaults 表，由 /rules/prompts 页面维护；这里只作最后一次兜底。
            self.default_prompt_versions = {
                "intent": "v1", "intent_user": "v1",
                "extraction": "v1", "extraction_user": "v1",
                "hotwords": "v2", "hotwords_user": "v2",
                "review_metrics": "v1",
            }


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

# 共享 KV（与 radar / console 同一个命名空间 RADAR_KV）：agent 直读直写，不调它们的 App 接口。
# 三个值来自 Render 环境变量，必须与 Cloudflare 侧 RADAR_KV 的 namespace id / 账户一致。
# 这是「存储介质依赖」而非「服务接口依赖」：agent 经 Cloudflare KV REST API 操作同一个 KV，
# 不经由 site-console 的 /api/hotwords 或 google-trend-radar 的 /api/serp、/api/trend。
CF_ACCOUNT_ID = env("CF_ACCOUNT_ID", "")
KV_NAMESPACE_ID = env("KV_NAMESPACE_ID", "")
CF_API_TOKEN = env("CF_API_TOKEN", "")
# 提示词真源库 radar-rules 的 D1 id（非 secret，与 console 的 RULES_DB 同一库）。
# site-agent 经 CF REST API 读 prompts / prompt_defaults；可用 PROMPTS_DB_ID 覆盖。
PROMPTS_DB_ID = env("PROMPTS_DB_ID", "dca72fb2-9090-429c-9698-d2086e6b49c8")
CONTACT_EMAIL = env("CONTACT_EMAIL", f"hello@{FLEET_DOMAIN}")
# /backfill 状态回填入口的令牌：配了才校验 X-Backfill-Token；留空与 /run 一样裸奔，
# 上线前必须填（或用网关鉴权把 /run 与 /backfill 一起挡在后面）。
BACKFILL_TOKEN = env("BACKFILL_TOKEN", "")

# 建站仓库的本地工作副本（git clone 落盘位置）
REPO_DIR = Path(env("AGENT_STATE_DIR", str(PROJECT_ROOT / "data"))) / "repos"
REPO_DIR.mkdir(parents=True, exist_ok=True)

# ---- LLM / 外部凭据 ----
LLM_API_KEY = env("LLM_API_KEY", "")
LLM_BASE_URL = env("LLM_BASE_URL", "")
LLM_MODEL = env("LLM_MODEL", "deepseek-flash")
# DeepSeek 思考模式默认开启：reasoning_content 会先吃掉 max_tokens，导致正文为空。
# auto（默认）= 仅当 base_url 指向 DeepSeek 时自动关闭；off = 强制关闭；on = 保留思考。
LLM_THINKING = env("LLM_THINKING", "auto")

# DeepSeek 平台后台「用量信息」页的登录态 token（可选，不是 API key）。
# 官方公开的账户接口只有 /user/balance（余额），消费 / 请求次数 / Tokens / 累计消费
# 只在 platform.deepseek.com 的私有面板接口里，需要浏览器 localStorage.userToken
# 的 value。拿到方式：登录后台 → DevTools Console 执行
#   copy(JSON.parse(localStorage.getItem('userToken')).value)
# 该 token 等同只读账单权限，按密码对待，绝不写进仓库；会过期，失效时
# /llm-quota 会降级成提示，余额仍照常显示。留空 = 只显示官方余额。
DEEPSEEK_PLATFORM_TOKEN = env("DEEPSEEK_PLATFORM_TOKEN", "")

# 后台「用量信息」按 API Key 维度返回多条 series，这个服务用的是哪一条。
# 账户里还有 Harness / workbuddy 等其它 key，卡片主数字只认本站这个 key，
# 免得把别的调用方的消费算进来。填平台下拉里显示的 key 名称即可。
DEEPSEEK_API_KEY_NAME = env("DEEPSEEK_API_KEY_NAME", "site-agent")
