# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""集中配置：环境变量 + 站点映射。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "templates"


def env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        raise RuntimeError(f"缺少必需环境变量：{key}")
    return value or ""


# 站群主域。本项目对「某个具体站群」的全部绑定都收敛到这一个变量：
# 子站 host、radar 地址、GSC 资源、外链话术里的域名，全部从这里派生。
# 换一个站群 = 改这一个变量 + SITES 清单 + 各项凭据，代码零修改。
FLEET_DOMAIN = env("FLEET_DOMAIN", "toolara.dev")


@dataclass(frozen=True)
class SiteConfig:
    """一个子站的部署目标（repo 名 = Worker 名 = 目录名）。

    host 只在需要完整域名时由 FLEET_DOMAIN 派生；本表本身不绑定任何具体域名。
    """

    name: str
    subdomain: str          # 子域前缀；空字符串表示主域本身
    repo_name: str
    brand: str = ""
    lang: str = "en"
    branch: str = "main"
    daily_limit: int = 5

    @property
    def host(self) -> str:
        return f"{self.subdomain}.{FLEET_DOMAIN}" if self.subdomain else FLEET_DOMAIN

    @property
    def repo(self) -> str:
        return f"{env('GH_ORG', required=True)}/{self.repo_name}"

    @property
    def url(self) -> str:
        return f"https://{self.host}"


# 子域前缀与仓库映射是「站群清单」，属部署配置而非架构绑定；
# repo_name 需与 GitHub 仓库名一致。换站群时改这份清单 + FLEET_DOMAIN。
SITES: dict[str, SiteConfig] = {
    s.name: s
    for s in [
        SiteConfig("music", "music", "ai-music-generator", "Toolara Music"),
        SiteConfig("figurine", "figurine", "ai-3d-figurine-generator"),
        SiteConfig("creative", "creative", "ai-creative-generator"),
        SiteConfig("background", "background", "ai-background-remover"),
        SiteConfig("audio", "audio", "ai-audio-tool"),
    ]
}

# radar 是站群内部的选题数据服务；地址默认从主域派生，也可整体覆盖
RADAR_URL = env("RADAR_URL", f"https://radar.{FLEET_DOMAIN}")

# 运营者联系方式（邮件发件人、外链采集 User-Agent 的 contact 字段）
CONTACT_EMAIL = env("CONTACT_EMAIL", "hello@toolara.dev")

STATE_DIR = Path(env("AGENT_STATE_DIR", str(PROJECT_ROOT / "data")))
STATE_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = STATE_DIR / "agent.sqlite"
REPO_DIR = STATE_DIR / "repos"
REPO_DIR.mkdir(parents=True, exist_ok=True)

# 业务上限
MAX_PAGES_PER_RUN = int(env("MAX_PAGES_PER_RUN", "5"))
MAX_LLM_CALLS_PER_DAY = int(env("MAX_LLM_CALLS_PER_DAY", "100"))
BREAKER_THRESHOLD = int(env("BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN_SEC = int(env("BREAKER_COOLDOWN_SEC", "1800"))
