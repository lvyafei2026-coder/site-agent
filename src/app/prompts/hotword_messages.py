# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""逐词分析的提示词拼装：按版本从 D1 取模板 → 填本词数据 → 出 system / user 两条消息。

措辞全部在 radar-rules D1 的 hotwords / hotwords_user 两 task 里（见本包 __init__.py），
本模块只做「加载 + 缓存 + 填占位符」，不判定结论——结论与信号标签的解析在
nodes/hotword_llm_node.py。

谁用它：nodes/hotword_llm_node.py（拼完直接交给 utils/llm_io.complete）。

★为什么不调 services/prompt_service.py 那个带日志的入口：本模块在 prompts 包里，
  反向依赖 services 就把分层倒过来了（见 test_architecture.py）。这里用同包的
  load_prompt，行为差别只少一条 prompt_loaded 日志，换来 lru_cache 后一轮 100 词
  只读一次 D1 快照。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Optional

from src.app.enums import PromptTask
from src.app.prompts import load_prompt

# 逐词分析要输出 8 小节、450 字以内的中文正文（约 600-900 token）；这是本线的输出
# 口径，与模板里的小节要求同源，所以跟拼装放在一起（传输层 utils/llm_io.complete 只收参数）。
MAX_TOKENS = 2000


@lru_cache(maxsize=8)
def _prompt_pair(version: Optional[str] = None) -> tuple[str, str]:
    """返回 (system, user 模板)。version 由 /run 的 prompt_versions.hotwords 透传。

    ★None 原样往下传，不要在本地先补成 settings 里的兜底版本：补成具体版本号就等于
    「显式指定」，D1 prompt_defaults 切默认后本线还钉在旧版（registry 自己会按
    「D1 默认 → config 兜底」解析）。
    """
    return (load_prompt(PromptTask.HOTWORDS.value, version),
            load_prompt(PromptTask.HOTWORDS_USER.value, version))


def examples_line(examples: list[dict[str, str]]) -> str:
    """把判例拼成 user 模板的 ``{examples_line}`` 占位内容；无判例返回空串。"""
    if not examples:
        return ""
    head = "以下是你过去判定「不做」的真实案例，遇到同类词请优先判不做："
    rows = [f"· {e['term']} — {e['reason']}" for e in examples]
    return head + "\n" + "\n".join(rows)


def build_messages(body: dict[str, Any], *, prompt_version: Optional[str] = None,
                   examples: Optional[list[dict[str, str]]] = None) -> list[dict[str, str]]:
    """拼 system + user；**措辞全部来自 D1 的 hotwords 提示词**，这里只填数据。

    SERP 前 6 的「域名 — 标题」是 AI 辨形的主要线索。模板由 _prompt_pair() 按
    prompt_version 一次性加载并缓存，一轮 100 词只读一次。

    ``examples`` 是人工判例反馈（见 wordlib.pick_examples）：仅当 user 模板含
    ``{examples_line}`` 占位符（v2+）时注入；v1 模板无此占位符则输出逐字不变。
    """
    system, user_template = _prompt_pair(prompt_version)

    term = str(body.get("term") or "")[:120]
    related = [str(x) for x in (body.get("related") or [])][:6]
    hints = "、".join(str(x) for x in (body.get("hints") or []))
    geo_count = body.get("geoCount") or 0
    traffic = body.get("traffic") or 0

    serp = [x for x in (body.get("serp") or []) if x][:6]
    lines = []
    for item in serp:
        if isinstance(item, str):
            lines.append("· " + item)
            continue
        domain = str(item.get("domain") or "")
        title = re.sub(r"\s+", " ", str(item.get("title") or ""))[:90]
        lines.append("· " + domain + (" — " + title if title else ""))
    serp_line = "现有搜索结果前几名（域名 — 页面标题）：\n" + "\n".join(lines) if lines else ""
    related_line = "关联查询：" + " / ".join(related) if related else "关联查询：（无）"
    hints_line = "命中的常青信号：" + hints if hints else "未命中明显常青信号"

    # 模板里没有 SERP 时是一整行空占位，过滤空行后与原拼装结果一致。
    fields = dict(term=term, related_line=related_line, serp_line=serp_line,
                  hints_line=hints_line, geo_count=geo_count, traffic=traffic)
    # ★只有模板真的含 {examples_line} 才传该字段：v1 模板没有它，传了会 KeyError。
    #   build_messages 的输出格式与 D1 模板占位符对应，见 test/test_prompts.py。
    if "{examples_line}" in user_template:
        fields["examples_line"] = examples_line(examples or [])
    user = "\n".join(x for x in user_template.format(**fields).split("\n") if x)
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]
