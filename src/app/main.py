# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""进程入口：`python -m src.app.main` 起 uvicorn，所有业务都走 HTTP。

    POST /run        用户线主图
    POST /hotwords   热词逐词循环图（前端按需触发，无常驻线程）

本模块只有 serve() 一件事，不带「单跑一轮」的第二个入口——那条路早先只为
`langgraph.json` 的 CLI 服务，线上从不走它，留着只会让入口有两套语义。
本地想看一轮结果直接 `curl -X POST localhost:8000/run`。
"""

from __future__ import annotations

import os

import uvicorn

from src.app.api.routes import api
from src.app.config import settings
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def serve() -> None:
    """常驻 HTTP 服务：主线程 uvicorn 承载 /run、/hotwords、/health 等所有入口。"""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    logger.info("service_start", extra={"host": host, "port": port})
    uvicorn.run(api, host=host, port=port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    serve()
