from __future__ import annotations

import functools
import os
from collections.abc import Callable

from src.app.config import settings
from src.app.utils.logger import get_logger

logger = get_logger(__name__)


def configure_langsmith() -> None:
    os.environ.setdefault("LANGSMITH_TRACING", str(settings.langsmith_tracing).lower())
    os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)
    if settings.langsmith_api_key:
        os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)


def traceable(name: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            configure_langsmith()
            logger.info("langsmith_trace", extra={"span": name, "enabled": settings.langsmith_tracing})
            return func(*args, **kwargs)

        return wrapper

    return decorator
