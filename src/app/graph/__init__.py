# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""编排层的统一门面：两条线在这里并列可见，入口（main / api）只依赖本包。

线 1｜对外用户输入  get_user_graph()  ← user_graph.py
线 2｜后台自动运行  run_batch()       ← hotword_batch.py（invoke hotword_graph.py 的循环图）

运维入口 run_backfill() 委托 services 层的状态回填（本包只做转发，不承载业务逻辑）。
入口层不再穿透到 tools / adapters：取图、驱动批次、回填都从 graph 走。
"""

from src.app.graph.hotword_batch import run_batch  # noqa: F401
from src.app.graph.hotword_graph import (  # noqa: F401
    build_hotword_graph, get_hotword_graph,
)
from src.app.graph.user_graph import build_user_graph, get_user_graph  # noqa: F401
from src.app.services.backfill_service import backfill_states

__all__ = [
    "get_user_graph", "build_user_graph",
    "get_hotword_graph", "build_hotword_graph",
    "run_batch", "run_backfill",
]


def run_backfill(*, limit: int = 200, dry_run: bool = False) -> dict:
    """运维回填的编排入口：转发给 services 层的 backfill_states。"""
    return backfill_states(limit=limit, dry_run=dry_run)
