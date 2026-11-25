# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""提示词的 D1 数据源（radar-rules 库）——运行时唯一真源。

★2026-11-17（用户定）：提示词从 site-agent 本地 versions/*.txt 全部迁到 radar-rules D1
（prompts / prompt_defaults 两表，与 console /rules 的规则库同一账号同一库），本地
txt 文件已删除。运行时只读 D1，/rules/prompts 页面改完最多 TTL_SECONDS 秒生效。

设计要点：
  - 一次查询拉全表（10 条量级），进程内缓存 60 秒；一轮 100 词也只走一次 HTTP。
  - D1 查询失败：有旧快照就继续用旧的（stale-while-error），线上 LLM 调用不受瞬时
    抖动影响；没有任何快照时 load() 返回 None，由注册表抛明确错误（不再有本地文件
    兜底）。
  - 未配 CF_ACCOUNT_ID / CF_API_TOKEN 时（本地开发）完全不碰网络，注册表按
    无快照处理并给出明确错误。

数据库 id：默认雷达规则库 radar-rules（dca72fb2…，非 secret，已在 wrangler.toml 公开）；
可用 PROMPTS_DB_ID 覆盖。CF_API_TOKEN 需要有该库的 D1 读权限。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from src.app.config import PROMPTS_DB_ID, env
from src.app.utils.logger import get_logger

logger = get_logger(__name__)

API = "https://api.cloudflare.com/client/v4"
TTL_SECONDS = 60

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
    """全表快照 + TTL 缓存：一次拉全表，60 秒内复用，失败退回旧快照。"""

    def __init__(self) -> None:
        self._fetcher = _http_fetch
        self._ttl = TTL_SECONDS
        self._lock = threading.Lock()
        # (fetched_at, {(task, version): content}, {task: version})
        self._cache: tuple[float, dict[tuple[str, str], str], dict[str, str]] | None = None

    @staticmethod
    def configured() -> bool:
        """有账号 + token 才走 D1；否则不发网络。"""
        return bool(env("CF_ACCOUNT_ID") and env("CF_API_TOKEN"))

    def snapshot(self) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
        """返回 (提示词字典, 默认版本字典)。命中缓存直接返回；失败降级旧快照/空。"""
        now = time.monotonic()
        with self._lock:
            cache = self._cache
        if cache and now - cache[0] < self._ttl:
            return cache[1], cache[2]

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
            return {}, {}                       # 注册表据此抛明确错误（无本地文件兜底）

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
        """提示词正文：D1 命中返回（去首尾空白），否则 None 让注册表回退默认版本/抛错。"""
        if not self.configured():
            return None
        prompts, _ = self.snapshot()
        text = prompts.get((task, version))
        return text.strip() if text and text.strip() else None

    def versions(self, task: str) -> list[str]:
        """列出某 task 在 D1 里的所有版本（按 vN 数字排序）。"""
        if not self.configured():
            return []
        prompts, _ = self.snapshot()
        found = [v for (t, v) in prompts if t == task]
        return sorted(found, key=lambda s: int(s[1:]) if s[1:].isdigit() else 0)


# 进程内单例：PromptRegistry 与 PromptService 共用同一份 60 秒快照。
prompt_store = D1PromptStore()
