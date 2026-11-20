# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""提示词加载（共用能力）：真源在 radar-rules D1，本层只负责取文案 + 留日志。

intent / extraction / metrics 三个实现类都从这里取 system 与 human 模板，
代码里不留任何提示词字面量（test/test_prompts.py 守着）。
"""

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
