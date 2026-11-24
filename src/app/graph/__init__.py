# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""编排层的统一门面：两条线在这里并列可见，入口（main / api）只依赖本包。

线 1｜常规运营  normal_graph.py   build_normal_graph / get_normal_graph / run_workflow  （POST /run）
线 2｜热词分析  hotword_graph.py  build_hotword_graph / get_hotword_graph / run_hotwords（POST /hotwords）
线 3｜热词创页  page_graph.py     build_page_graph / get_page_graph / run_page_build（POST /page）

两条线互不嵌套、各有独立 API，但**机制同构**：装配 → 单例 → initial_*_state →
run_traced（共用驱动器 + 各自快照）→ 终态 + trace + langsmith + traceRun。
差别只在节点类型、state 内容、是否带 checkpointer。进程内没有后台线程或定时循环。
入口层不再穿透到 tools / adapters：取图、驱动两条线都从本包走。
"""

from src.app.graph.hotword_graph import (  # noqa: F401
    build_hotword_graph, get_hotword_graph, run_hotwords,
)
from src.app.graph.normal_graph import (  # noqa: F401
    build_normal_graph, get_normal_graph, run_workflow,
)
from src.app.graph.page_graph import (  # noqa: F401
    build_page_graph, get_page_graph, run_page_build,
)

__all__ = [
    "get_normal_graph", "build_normal_graph", "run_workflow",
    "get_hotword_graph", "build_hotword_graph", "run_hotwords",
    "get_page_graph", "build_page_graph", "run_page_build",
]
