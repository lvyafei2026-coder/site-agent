"""发给 LLM 的所有文本，唯一存放处 = ``versions/<task>/vN.txt``。

★2026-11-13（用户定）：原先 ``tools/adapters/sources/hotwords.py`` 里硬编码的
  ``AI_SYSTEM``（1600 余字）与逐词 user 模板、两个节点里的 human 字面量，全部收拢到这里。
  业务代码只允许调用 :func:`load_prompt`，**不允许再出现整段提示词字面量**
  （``test/test_prompts.py`` 会静态扫描守着）。

约定：
  - 一个 task 一个目录，一个版本一个 ``vN.txt``；加载时缺版本自动回退 v1（见 registry.py）。
  - 改措辞 = **新增一个版本文件**，不要直接改已上线的 vN.txt；再切默认版本
    （``config.default_prompt_versions``）或在 ``/run`` 里传 ``prompt_versions``。
  - ``*_user`` 是同一任务的 human 模板，与 system 分开存，方便单独调。
  - 只有 ``intent`` / ``extraction`` / ``hotwords`` 走 ``/run`` 的版本覆盖；
    ``*_user`` 固定跟随 v1（换 system 版本时 human 保持同一格式）。

``services/prompt_service.py`` 的 ``load_prompt`` 是**带日志**的节点入口；本模块的是
不含业务逻辑的纯加载函数，供工具层（adapters 不能反向依赖 services）直接使用。
"""

from __future__ import annotations

from .registry import PromptRegistry

# task → 用途。清单即目录清单，新增提示词时同步这里（测试会双向校验）。
PROMPT_TASKS: dict[str, str] = {
    "intent": "意图分类 system（现有 v1 / v2）",
    "intent_user": "意图分类 human 模板",
    "extraction": "字段抽取 system",
    "extraction_user": "字段抽取 human 模板",
    "hotwords": "逐词站点方案 system（8 小节 + 辨形/真人/作品红线，原 radar/src/ai.js 的 AI_SYSTEM）",
    "hotwords_user": "逐词站点方案 human 模板（关键词 / 关联查询 / SERP 标题 / 信号 / 地区 / 热度）",
}

_registry = PromptRegistry()


def load_prompt(task: str, version: str | None = None) -> str:
    """读 ``versions/<task>/<version>.txt``，去掉首尾空白后原样返回。

    version 为 None 时由 PromptRegistry 解析：请求覆盖 → settings 默认 → v1。
    """
    return _registry.load(task, version)


def available_versions(task: str) -> list[str]:
    """列出某 task 已有版本（按 vN 数字排序），用于运维核对。"""
    path = _registry.root / task
    if not path.is_dir():
        return []
    return sorted((p.stem for p in path.glob("v*.txt")),
                  key=lambda s: int(s[1:]) if s[1:].isdigit() else 0)
