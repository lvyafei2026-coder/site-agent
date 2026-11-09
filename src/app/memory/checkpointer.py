"""Checkpointer 后端：memory（默认）/ sqlite（Render 持久盘）。

- memory：进程内，重启即丢，适合本地与测试；
- sqlite：落盘，配合 Render 的 /data 持久卷。

注意：`SqliteSaver.from_conn_string()` 返回的是**上下文管理器**，不是 saver 实例，
直接把它的返回值传给 `compile(checkpointer=...)` 会在第一次写入时炸，所以这里自行
建连接再交给构造函数（sqlite3 连接需 check_same_thread=False，FastAPI 是多线程）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.app.config import settings
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def get_checkpointer(backend: str | None = None, database_url: str | None = None):
    backend = (backend or settings.memory_backend).lower()
    url = settings.database_url if database_url is None else database_url

    if backend == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        db_path = url.replace("sqlite:///", "") or "./state.db"
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        logger.info("checkpointer_backend", extra={"backend": "sqlite", "path": db_path})
        return SqliteSaver(sqlite3.connect(db_path, check_same_thread=False))

    if backend != "memory":
        raise ValueError(f"不支持的 MEMORY_BACKEND：{backend}（memory | sqlite）")

    from langgraph.checkpoint.memory import MemorySaver

    logger.info("checkpointer_backend", extra={"backend": "memory"})
    return MemorySaver()
