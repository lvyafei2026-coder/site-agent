# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""图组装：sense → decide → (execute → verify) | learn。"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .config import CHECKPOINT_PATH
from .nodes.decide import decide
from .nodes.execute import execute
from .nodes.learn import learn
from .nodes.sense import sense
from .nodes.verify import verify
from .state import OpsState


def route_after_decide(state: OpsState) -> str:
    """有动作才执行，否则直接记录结论。"""
    return "execute" if state.get("actions") else "learn"


def build_graph() -> StateGraph:
    g = StateGraph(OpsState)
    g.add_node("sense", sense)
    g.add_node("decide", decide)
    g.add_node("execute", execute)
    g.add_node("verify", verify)
    g.add_node("learn", learn)

    g.add_edge(START, "sense")
    g.add_edge("sense", "decide")
    g.add_conditional_edges(
        "decide", route_after_decide, {"execute": "execute", "learn": "learn"}
    )
    g.add_edge("execute", "verify")
    g.add_edge("verify", "learn")
    g.add_edge("learn", END)
    return g


@lru_cache(maxsize=1)
def get_app():
    """进程内单例。checkpointer 落盘到持久卷，进程重启后可续跑。"""
    conn = sqlite3.connect(str(CHECKPOINT_PATH), check_same_thread=False)
    return build_graph().compile(checkpointer=SqliteSaver(conn))
