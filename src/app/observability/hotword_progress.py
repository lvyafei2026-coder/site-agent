# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线单轮进度：进程内「最近一轮跑到哪了」，给 /hotwords 的轮询端读。

    POST /hotwords        入口占位（已在跑 → 409）→ 起后台线程 → 立即返回
    落账一个词            collect_word 调 report() 更新计数
    线程收口              finish(summary) / fail(error)
    GET  /hotwords/status snapshot() 原样回给页面

只记最近一轮；start() 靠 updatedAt 判活——超过 STALE_SEC 没更新就当进程已死，
允许重新发起。进程重启丢的只是进度展示，逐词结果一直在 KV 的 AI 缓存里。
为什么放内存不放 KV：轮询要 3 秒级的新鲜度，KV 最终一致会滞后（约 60s），
而且共享 KV 免费档每天只有 1000 次写，逐词再写一份进度不划算。
"""

from __future__ import annotations

import threading
import time
from typing import Any

IDLE = "idle"
RUNNING = "running"
DONE = "done"
ERROR = "error"

STALE_SEC = 600    # 一个词最坏也就 90 秒；10 分钟没有更新说明进程已经不在了


class HotwordProgress:
    """一轮的进度账本。并发靠一把锁，读写都是同进程内存操作，不含 IO。"""

    STALE_SEC = STALE_SEC

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"status": IDLE}

    def start(self, limit: int) -> bool:
        """占住本轮；已有一轮在跑（且没超时）返回 False，由入口回 409。"""
        with self._lock:
            if self._state["status"] == RUNNING and \
                    time.time() - self._state.get("updatedAt", 0) < STALE_SEC:
                return False
            now = time.time()
            self._state = {
                "status": RUNNING, "limit": int(limit),
                "startedAt": now, "updatedAt": now,
                "done": 0, "analyzed": 0, "failed": 0, "term": "",
                "summary": None, "error": "",
            }
            return True

    def report(self, results: list[dict[str, Any]]) -> None:
        """每落账一个词调一次；不在 running 时是空操作（同步调用 / 测试不带进度）。"""
        with self._lock:
            if self._state.get("status") != RUNNING:
                return
            analyzed = sum(1 for r in results if not r.get("error"))
            last = results[-1] if results else {}
            self._state.update({
                "done": len(results),
                "analyzed": analyzed,
                "failed": len(results) - analyzed,
                "term": str(last.get("term") or ""),
                "updatedAt": time.time(),
            })

    def finish(self, summary: dict[str, Any]) -> None:
        with self._lock:
            if self._state.get("status") != RUNNING:
                return
            self._state.update({"status": DONE, "summary": summary,
                                "updatedAt": time.time()})

    def fail(self, error: str) -> None:
        with self._lock:
            if self._state.get("status") != RUNNING:
                return
            self._state.update({"status": ERROR, "error": error,
                                "updatedAt": time.time()})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._state)
        if out.get("status") == RUNNING:
            out["elapsedMs"] = int((time.time() - out.get("startedAt", 0)) * 1000)
        return out


hotword_progress = HotwordProgress()
