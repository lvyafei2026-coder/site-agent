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


@dataclass(frozen=True)
class SiteConfig:
    """一个子站的部署目标（repo 名 = Worker 名 = 目录名）。"""

    name: str
    host: str
    repo_name: str
    brand: str = "Toolara"
    lang: str = "en"
    branch: str = "main"
    daily_limit: int = 5

    @property
    def repo(self) -> str:
        return f"{env('GH_ORG', required=True)}/{self.repo_name}"

    @property
    def url(self) -> str:
        return f"https://{self.host}"


# host 以 tool-proxy 的 DOMAIN_MAP 为准；repo_name 需与 GitHub 仓库名一致。
SITES: dict[str, SiteConfig] = {
    s.name: s
    for s in [
        SiteConfig("music", "music.toolara.dev", "ai-music-generator", "Toolara Music"),
        SiteConfig("figurine", "figurine.toolara.dev", "ai-3d-figurine-generator"),
        SiteConfig("creative", "creative.toolara.dev", "ai-creative-generator"),
        SiteConfig("background", "background.toolara.dev", "ai-background-remover"),
        SiteConfig("audio", "audio.toolara.dev", "ai-audio-tool"),
    ]
}

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
