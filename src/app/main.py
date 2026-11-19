# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""CLI 入口：两条线在这里并列可见，共用底层工具/适配器，图结构各管各的。

线 1｜对外用户输入（用户线）  run()
    get_graph()（builder.py 主图）：classify_intent → run_processing → validate_result → output
    由 HTTP /run（api/routes.py）与 CLI 默认命令驱动；按 thread_id 续跑。

线 2｜后台自动运行（后台线）  run_background()
    get_hotword_graph()（builder.py 热词循环图）：fetch_one → analyze_one → should_continue 回环
    读一词→分析→回环，直到词用完或达 limit。经 hotwords.run_batch 复用批次 trace 与汇总；
    由外部调度器（Render cron 等）周期调用，单次 invoke 跑完即弃。

两图在 graph/builder.py 并列装配，互不嵌套。节点逻辑不动——入口只做分发。

用法：
    python -m src.app.main               # 用户线：跑一轮示例 prompt
    python -m src.app.main background     # 后台线：自动分析候选池未跟进的热词
"""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

from src.app.graph.builder import get_graph
from src.app.graph.state import initial_state
from src.app.observability.metrics import metrics
from src.app.observability.trace import run_traced
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PROMPT = "查看站群近期热词、新页与点击数据，给出本轮运营结论。"


# ══ 线 1：对外用户输入 ═══════════════════════════════════════════════
def run() -> dict:
    """用户线：跑一轮主图（意图分类→取数→校验→输出）。"""
    result, trace = run_traced(
        get_graph(),
        initial_state(DEFAULT_PROMPT),
        config={
            "configurable": {"thread_id": f"cli-{int(time.time())}"},
            "run_name": "site-agent:cli",
            "tags": ["site-agent", "cli"],
        },
    )
    logger.info("workflow_complete",
                extra={"intent": result.get("intent"), "path": trace["path"],
                       "totalMs": trace["totalMs"], "metrics": metrics.snapshot()})
    return result


# ══ 线 2：后台自动运行 ═══════════════════════════════════════════════
def run_background(limit: int = 20,
                   prompt_version: Optional[str] = None) -> dict[str, Any]:
    """后台线：驱动热词循环图，逐词分析候选池里未跟进的热词。

    走 hotwords.run_batch 复用批次 trace 外壳与结果汇总；其执行体经加载期边界直连
    builder.get_hotword_graph()（热词循环图，与用户线主图并列、互不嵌套）。
    与用户线共用同一套工具/适配器，只是入口图不同。返回结构与 analyze_hotwords 一致。

    延迟导入：用户线不需要拉起整条热词适配器链，后台线被调度时才加载。
    """
    from src.app.tools.adapters.terms import hotwords

    out = hotwords.run_batch(limit=limit, prompt_version=prompt_version)
    logger.info("background_complete",
                extra={"analyzed": out.get("analyzed"), "failed": out.get("failed"),
                       "ok": out.get("ok"), "metrics": metrics.snapshot()})
    return out


def main(argv: Optional[list[str]] = None) -> dict:
    """CLI 分发：默认用户线；`background` 子命令跑后台线。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "background":
        return run_background()
    return run()


if __name__ == "__main__":
    main()
