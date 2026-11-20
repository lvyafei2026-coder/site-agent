# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""流量复盘分析（review_metrics 专属的共用能力）：比对各站埋点 / GSC / 页面数据。

由 processing_service 在取数成功后调用；返回 markdown 正文（结论 + 表现好的站点
排名 + 理由）。无 key 或调用失败返回空串，上层跳过该段，不阻塞整轮。
提示词来自 D1 的 review_metrics task，不在代码里硬编码（test/test_prompts.py 守着）。
"""

from __future__ import annotations

import json

from src.app.enums import PromptTask
from src.app.llm.completion import complete_text
from src.app.observability.langsmith import traceable
from src.app.services.prompt_service import prompt_service

MAX_PAYLOAD_CHARS = 6000


def _compact_payload(results: dict) -> str:
    """把工具结果压成 LLM 易读的精简 JSON：只取 ok 工具的 result 部分，过长截断。

    review_metrics 的数据可能很大（整张候选表 / 全站埋点），这里只保留
    get_click_stats（by_site 透视表）/ get_search_analytics / get_page_inventory
    的核心字段，避免把上报撑爆。
    """
    slim: dict = {}
    for name, out in (results or {}).items():
        if isinstance(out, dict) and out.get("ok"):
            slim[name] = out.get("result")
    try:
        text = json.dumps(slim, ensure_ascii=False, default=str)
    except TypeError:
        text = str(slim)
    if len(text) > MAX_PAYLOAD_CHARS:
        text = text[:MAX_PAYLOAD_CHARS] + f"...<truncated {len(text) - MAX_PAYLOAD_CHARS} chars>"
    return text


class MetricsService:
    @traceable(name="analyze_metrics")
    def analyze(self, results: dict, versions: dict[str, str] | None = None) -> str:
        versions = versions or {}
        system = prompt_service.load_prompt(
            PromptTask.REVIEW_METRICS.value,
            version=versions.get(PromptTask.REVIEW_METRICS.value))
        return complete_text(system, _compact_payload(results),
                             warn_event="analyze_metrics_failed")


metrics_service = MetricsService()