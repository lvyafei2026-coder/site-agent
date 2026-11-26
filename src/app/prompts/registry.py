from __future__ import annotations

from src.app.config import settings
from src.app.prompts.d1_source import prompt_store
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def _version_num(version: str) -> int:
    tail = str(version or "")[1:]
    return int(tail) if tail.isdigit() else -1


class PromptRegistry:
    """提示词加载：D1 唯一真源（radar-rules 库 prompts / prompt_defaults 表）。

    ★2026-11-17：本地 versions/*.txt 已全部删除，正文与默认版本都只读 D1。
      /rules/prompts 页面改完最多 60 秒生效、不用发版；读库失败时用进程内旧快照
      （stale-while-error）兜住瞬时抖动。只有「首次拉取就失败且没有任何快照」才
      抛错——这时应去 /rules/prompts 检查库与 CF_API_TOKEN，而不是静默用空提示词
      让 LLM 乱答。
    """

    def __init__(self) -> None:
        pass

    def resolve_version(self, task: str, version: str | None = None) -> str:
        if version:
            return version
        fallback = settings.default_prompt_versions.get(task, "v1")
        return prompt_store.resolve_version(task, fallback)

    def load(self, task: str, version: str | None = None) -> str:
        selected = self.resolve_version(task, version)
        text = prompt_store.load(task, selected)
        if text is not None:
            return text
        # 显式指定了版本但 D1 里没有：回退到该 task 的默认版本（容错，不因版本号写错崩批）
        fallbacks: list[str] = []
        if version:
            default = self.resolve_version(task, None)
            if default != selected:
                fallbacks.append(default)
        # 唯一更新版本兜底：/rules/prompts 新建了 vN 却没设默认、或迁移 SQL 未插该 task 的
        # 默认行时，config 默认版本可能不存在。task 只有一个版本、且它比想要的版本新，就
        # 明确用它并留日志；更旧的不碰（word_plan_page v1 是 markdown 版，拿它兜底会让
        # 排产解析不到结构化字段），多版本也不猜——猜错版本的代价比失败一轮更大。
        available = prompt_store.versions(task)
        if len(available) == 1 and available[0] not in [selected, *fallbacks]:
            if _version_num(available[0]) > _version_num(selected):
                fallbacks.append(available[0])
        for fb in fallbacks:
            text = prompt_store.load(task, fb)
            if text is not None:
                logger.warning("prompt_version_fallback", extra={
                    "task": task, "wanted": selected, "used": fb})
                return text
        raise RuntimeError(
            f"提示词 {task}/{selected} 无法从 D1 读取；"
            "请在 /rules/prompts 检查 prompts / prompt_defaults 表与 CF_API_TOKEN 的 D1 读权限"
        )

    def available_versions(self, task: str) -> list[str]:
        """列出某 task 在 D1 里已有的版本（按 vN 数字排序）。"""
        return prompt_store.versions(task)
