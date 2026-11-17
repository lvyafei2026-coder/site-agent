"""发给 LLM 的所有文本：唯一真源 = radar-rules D1 的 prompts / prompt_defaults 表。

★2026-11-17（用户定）：提示词迁到 D1，由 site-console 的 /rules/prompts 页面维护。
  运行时 PromptRegistry 先读 D1（60 秒缓存），读不到才回退本地 versions/<task>/vN.txt。
  本地 txt 从「唯一真源」降级为「回退种子 / 迁移基线」，日常改提示词不用发版。
  最初（2026-11-13）这些文本原本散落在 hotwords.py 的 AI_SYSTEM 等处，收拢到本目录；
  现在目录仍在，作为 D1 不可用时的兜底 + test_prompts.py 的逐字 SHA 基线。
  业务代码只允许调用 :func:`load_prompt`，**不允许再出现整段提示词字面量**
  （``test/test_prompts.py`` 会静态扫描守着）。

约定：
  - 一个 task 一个目录，一个版本一个 ``vN.txt``；D1 缺失时回退本地文件（缺版本再回退 v1）。
  - 改措辞 = 在 /rules/prompts 页面新增一个版本（如 v3）并切默认，保留历史便于回滚；
    迁移种子 SQL 见 site-console/migrations/0002_prompts.sql。
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
    "review_metrics": "review_metrics 意图的站点表现分析 system：把各站埋点/GSC/页面数据交给 LLM 比对，识别表现好的站点（提示词集中此处，禁止在代码里写整段提示词）",
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
