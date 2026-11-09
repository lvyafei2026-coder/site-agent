"""进程内日志：单行 JSON，便于 Render / Cloudflare 侧按字段检索。"""

from __future__ import annotations

import json
import logging

from src.app.config import settings

# LogRecord 自带字段的基准集合：只构造一次，避免每条日志都新建对象。
_BASE_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = {k: v for k, v in record.__dict__.items() if k not in _BASE_FIELDS}
        if extra:
            payload["extra"] = extra
        # default=str：字典 / 异常等非 JSON 原生类型不能让日志本身抛错
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.propagate = False
    return logger
