# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""CLI 入口：python -m src.app.main，跑一轮并输出结果。"""

from __future__ import annotations

import time

from src.app.graph.builder import get_graph
from src.app.graph.state import initial_state
from src.app.observability.metrics import metrics
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PROMPT = "查看站群近期热词、新页与点击数据，给出本轮运营结论。"


def run():
    result = get_graph().invoke(
        initial_state(DEFAULT_PROMPT),
        config={
            "configurable": {"thread_id": f"cli-{int(time.time())}"},
            "run_name": "site-agent:cli",
            "tags": ["site-agent", "cli"],
        },
    )
    logger.info("workflow_complete",
                extra={"intent": result.get("intent"), "metrics": metrics.snapshot()})
    return result


if __name__ == "__main__":
    run()
