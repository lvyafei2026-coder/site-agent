from __future__ import annotations

from pathlib import Path

from src.app.config import settings


class PromptRegistry:
    def __init__(self) -> None:
        self.root = Path(settings.prompt_root)

    def resolve_version(self, task: str, version: str | None = None) -> str:
        return version or settings.default_prompt_versions.get(task, "v1")

    def load(self, task: str, version: str | None = None) -> str:
        selected = self.resolve_version(task, version)
        prompt_path = self.root / task / f"{selected}.txt"
        if not prompt_path.exists():
            prompt_path = self.root / task / "v1.txt"
        return prompt_path.read_text(encoding="utf-8").strip()
