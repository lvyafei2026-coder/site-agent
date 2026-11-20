# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""编排层的统一门面：两条线在这里并列可见，入口（main / api）只依赖本包。

线 1｜用户输入  get_user_graph()  ← user_graph.py（POST /run）
线 2｜热词分析  run_hotwords()    ← hotword_graph.py（循环图 + 运行入口，POST /hotwords）

两条线互不嵌套、各有独立 API，由前端按需触发；进程内没有后台线程或定时循环。
入口层不再穿透到 tools / adapters：取图、驱动热词图都从本包走。
"""

from src.app.graph.hotword_graph import (  # noqa: F401
    build_hotword_graph, get_hotword_graph, run_hotwords,
)
from src.app.graph.user_graph import build_user_graph, get_user_graph  # noqa: F401

__all__ = [
    "get_user_graph", "build_user_graph",
    "get_hotword_graph", "build_hotword_graph",
    "run_hotwords",
]
