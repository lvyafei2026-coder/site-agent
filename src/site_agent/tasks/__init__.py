# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""任务注册表：每个任务模块暴露统一接口，节点层按 task_type 分发。

接口约定（模块级）：
    NAME          str   任务名，与 OpsState.task_type 一致
    LLM_PROFILE   str   gateways.PROFILES 的键
    PROMPT        str   llm/prompts/<PROMPT>.md
    sense(state) -> list[dict]
    build_user_prompt(state) -> str
    parse(decision, state) -> list[Action]
    execute_action(action) -> dict
    verify(state) -> list[Check]
    learn(state) -> dict
"""

from __future__ import annotations

from types import ModuleType

from . import backlink, daily_report, gen_page, promo

MODULES: dict[str, ModuleType] = {
    m.NAME: m for m in (gen_page, daily_report, backlink, promo)
}


def get_task(name: str) -> ModuleType:
    try:
        return MODULES[name]
    except KeyError:
        raise KeyError(f"未知任务类型：{name}（可选：{', '.join(MODULES)}）") from None
