# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""提示词的 D1 数据源（radar-rules 库）——运行时唯一真源。

★2026-11-17（用户定）：提示词从 site-agent 本地 versions/*.txt 迁到 radar-rules D1
（prompts / prompt_defaults 两表，与 console /rules 的规则库同一账号同一库）。运行时
优先读 D1，页面改完最多 TTL_SECONDS 秒生效；本地 txt 只在 D1 未配置 / 读不到时回退。

设计要点：
  - 一次查询拉全表（10 条量级），进程内缓存 60 秒；一批 20 词只走一次 HTTP。
  - D1 查询失败：有旧快照就继续用旧的（stale-while-error），没有就返回空让注册表
    回退本地文件——★任何情况下都不让线上 LLM 调用因 D1 抖动而崩。
  - 未配 CF_ACCOUNT_ID / CF_API_TOKEN 时（开发、测试环境）完全不碰网络，纯走本地文件，
    因此 test_prompts.py 的逐字 SHA 基线仍是对本地文件的校验，不受线上改动影响。

数据库 id：默认雷达规则库 radar-rules（dca72fb2…，非 secret，已在 wrangler.toml 公开）；
可用 PROMPTS_DB_ID 覆盖。CF_API_TOKEN 需要有该库的 D1 读权限。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import requests

from src.app.config import PROMPTS_DB_ID
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

API = "https://api.cloudflare.com/client/v4"
TTL_SECONDS = 60

# 注入点：测试传 fetcher 返回 {"prompts": [...], "defaults": [...]}，不碰网络。
Fetcher = Callable[[str, str, str], dict[str, list[dict[str, Any]]]]


def _http_fetch(account_id: str, api_token: str, database_id: str) -> dict[str, list[dict[str, Any]]]:
    url = f"{API}/accounts/{account_id}/d1/database/{database_id}/query"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    out: dict[str, list[dict[str, Any]]] = {}
    for key, sql in (
        ("prompts", "SELECT task, version, content FROM prompts"),
        ("defaults", "SELECT task, version FROM prompt_defaults"),
    ):
        resp = requests.post(url, headers=headers, json={"sql": sql, "params": []}, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            raise RuntimeError(f"D1 提示词查询失败({key}): {body.get('errors')}")
        result = body.get("result") or []
        out[key] = (result[0].get("results") or []) if result else []
    return out


class D1PromptStore:
    """全表快照 + TTL 缓存。configurable 通过 fetcher 注入 fake 做单测。"""

    def __init__(self, fetcher: Fetcher | None = None, ttl: int = TTL_SECONDS) -> None:
        self._fetcher = fetcher or _http_fetch
        self._ttl = ttl
        self._lock = threading.Lock()
        # (fetched_at, {(task, version): content}, {task: version})
        self._cache: tuple[float, dict[tuple[str, str], str], dict[str, str]] | None = None

    @staticmethod
    def configured() -> bool:
        """有账号 + token 才走 D1；否则纯本地文件（本地开发 / 测试）。"""
        from src.app.config import env
        return bool(env("CF_ACCOUNT_ID") and env("CF_API_TOKEN"))

    def clear(self) -> None:
        with self._lock:
            self._cache = None

    def snapshot(self) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
        """返回 (提示词字典, 默认版本字典)。命中缓存直接返回；失败降级旧快照/空。"""
        now = time.monotonic()
        with self._lock:
            cache = self._cache
        if cache and now - cache[0] < self._ttl:
            return cache[1], cache[2]

        from src.app.config import env
        try:
            data = self._fetcher(env("CF_ACCOUNT_ID"), env("CF_API_TOKEN"), PROMPTS_DB_ID)
            prompts = {
                (str(r["task"]), str(r["version"])): str(r["content"])
                for r in data.get("prompts", [])
                if r.get("task") and r.get("version") and r.get("content") is not None
            }
            defaults = {
                str(r["task"]): str(r["version"])
                for r in data.get("defaults", [])
                if r.get("task") and r.get("version")
            }
        except Exception as e:
            logger.warning("prompt_d1_fetch_failed", extra={"error": str(e)})
            if cache:
                return cache[1], cache[2]      # stale-while-error
            return {}, {}                       # 让注册表回退本地文件

        with self._lock:
            self._cache = (now, prompts, defaults)
        return prompts, defaults

    def resolve_version(self, task: str, fallback: str) -> str:
        """默认版本：D1 prompt_defaults 优先，取不到用传入的 config 默认值。"""
        if not self.configured():
            return fallback
        _, defaults = self.snapshot()
        return defaults.get(task) or fallback

    def load(self, task: str, version: str) -> str | None:
        """提示词正文：D1 命中返回（去首尾空白），否则 None 让注册表回退本地文件。"""
        if not self.configured():
            return None
        prompts, _ = self.snapshot()
        text = prompts.get((task, version))
        return text.strip() if text and text.strip() else None


# 进程内单例：PromptRegistry 与 PromptService 共用同一份 60 秒快照。
prompt_store = D1PromptStore()
