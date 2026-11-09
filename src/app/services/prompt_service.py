from __future__ import annotations

from src.app.prompts.registry import PromptRegistry
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


class PromptService:
    def __init__(self) -> None:
        self.registry = PromptRegistry()

    def load_prompt(self, task: str, version: str | None = None) -> str:
        resolved = self.registry.resolve_version(task, version)
        text = self.registry.load(task, resolved)
        logger.info("prompt_loaded", extra={"task": task, "version": resolved})
        return text


prompt_service = PromptService()
