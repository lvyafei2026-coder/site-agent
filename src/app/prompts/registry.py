from __future__ import annotations

from pathlib import Path

from src.app.config import settings
from src.app.prompts.d1_source import prompt_store


class PromptRegistry:
    """提示词加载：D1 优先（唯一真源），失败/未配置回退本地 versions/*.txt。

    ★2026-11-17：运行时正文与默认版本都先问 D1（radar-rules 库），本地文件降级为
     种子副本。这样 /rules/prompts 页面改完最多 60 秒生效、不用发版；D1 抖动时
     本地文件顶上，线上 LLM 调用不受影响。测试环境无 CF 凭据 → 纯本地，逐字 SHA
     基线照旧校验本地文件。
    """

    def __init__(self) -> None:
        self.root = Path(settings.prompt_root)

    def resolve_version(self, task: str, version: str | None = None) -> str:
        if version:
            return version
        fallback = settings.default_prompt_versions.get(task, "v1")
        return prompt_store.resolve_version(task, fallback)

    def load(self, task: str, version: str | None = None) -> str:
        selected = self.resolve_version(task, version)
        remote = prompt_store.load(task, selected)
        if remote is not None:
            return remote
        prompt_path = self.root / task / f"{selected}.txt"
        if not prompt_path.exists():
            prompt_path = self.root / task / "v1.txt"
        return prompt_path.read_text(encoding="utf-8").strip()
