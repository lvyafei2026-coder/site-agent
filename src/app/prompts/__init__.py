"""发给 LLM 的所有文本：唯一真源 = radar-rules D1 的 prompts / prompt_defaults 表。

★2026-11-17（用户定）：提示词全部迁到 D1，由 site-console 的 /rules/prompts 页面维护；
  本地 versions/<task>/vN.txt 已删除。运行时 PromptRegistry 只读 D1（60 秒缓存 +
  失败旧快照兜底），页面改完不用发版。最初（2026-11-13）这些文本散落在 hotwords.py
  的 AI_SYSTEM 等处，先收拢到本目录，再整体搬进 D1。
  业务代码只允许调用 :func:`load_prompt`，**不允许再出现整段提示词字面量**
  （``test/test_prompts.py`` 会静态扫描守着）。

约定：
  - 一个 task 可有多版本（v1/v2…），正文与默认版本都在 D1 的 prompts / prompt_defaults
    两表；版本不存在时回退该 task 的默认版本。
  - 改措辞 = 在 /rules/prompts 页面新增一个版本（如 v3）并切默认，保留历史便于回滚。
  - ``*_user`` 是同一任务的 human 模板，与 system 分开存，方便单独调。
  - 只有 ``intent`` / ``extraction`` / ``hotwords`` 走 ``/run`` 的版本覆盖；
    ``*_user`` 固定跟随默认版本（换 system 版本时 human 保持同一格式）。

``services/prompt_service.py`` 的 ``load_prompt`` 是**带日志**的节点入口；本模块的是
不含业务逻辑的纯加载函数，供本包的 ``hotword_messages.py``（逐词提示词拼装）直接使用——
prompts 是横切包，反向依赖 services 就把分层倒过来了。
"""

from __future__ import annotations

from .registry import PromptRegistry

# task → 用途。清单即 D1 prompts 表里应有的 task（测试校验默认版本覆盖此清单）。
PROMPT_TASKS: dict[str, str] = {
    "intent": "意图分类 system（现有 v1 / v2）",
    "intent_user": "意图分类 human 模板",
    "extraction": "字段抽取 system",
    "extraction_user": "字段抽取 human 模板",
    "hotwords": "逐词站点方案 system（8 小节 + 辨形/真人/作品红线，原 radar/src/ai.js 的 AI_SYSTEM）",
    "hotwords_user": "逐词站点方案 human 模板（关键词 / 关联查询 / SERP 标题 / 信号 / 地区 / 热度）",
    "review_metrics": "review_metrics 意图的站点表现分析 system：把各站埋点/GSC/页面数据交给 LLM 比对，识别表现好的站点（提示词集中此处，禁止在代码里写整段提示词）",
    "promo_article": "外链文章起稿 system：Dev.to 推广文（标题/tags/target_url/draft 的 JSON，正文一律英文）",
    "word_plan_page": "热词创页排产 system：待开发词 × 近 30 天新页 × 历史复盘 → 本轮排产方案",
    "page_content": "建页正文写作 system：把内部选词分析稿改写成访客可读的页面正文（禁分析元话语）",
}

_registry = PromptRegistry()


def load_prompt(task: str, version: str | None = None) -> str:
    """从 D1 读提示词正文，去掉首尾空白后原样返回。

    version 为 None 时由 PromptRegistry 解析：请求覆盖 → D1 默认 → config 默认。
    D1 读不到（且无旧快照）时抛 RuntimeError，提示去 /rules/prompts 检查。
    """
    return _registry.load(task, version)


def available_versions(task: str) -> list[str]:
    """列出某 task 在 D1 已有版本（按 vN 数字排序），用于运维核对。"""
    return _registry.available_versions(task)
