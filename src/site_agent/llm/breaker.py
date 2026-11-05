# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""熔断器：连续失败达到阈值后写 KV 暂停该作用域，冷却期内拒绝执行。"""

from __future__ import annotations

import logging
import time

from ..cf import kv
from ..config import BREAKER_COOLDOWN_SEC, BREAKER_THRESHOLD
from ..notify.mail import send_alert

log = logging.getLogger(__name__)

_failures: dict[str, int] = {}


class BreakerPaused(RuntimeError):
    def __init__(self, scope: str, flag: dict | None = None) -> None:
        super().__init__(f"{scope} 处于熔断冷却期：{flag}")
        self.scope = scope
        self.flag = flag or {}


def ensure_alive(scope: str) -> None:
    """熔断器自身故障时 fail-open：读不到标记就按未熔断放行。"""
    try:
        flag = kv.get(f"pause:{scope}")
    except Exception:  # noqa: BLE001 - 保护装置不应阻断业务
        log.warning("读熔断标记失败，按未熔断放行 scope=%s", scope)
        return
    if flag:
        raise BreakerPaused(scope, flag)


def record(scope: str, ok: bool, error: str | None = None) -> None:
    if ok:
        _failures[scope] = 0
        return
    count = _failures.get(scope, 0) + 1
    _failures[scope] = count
    if count >= BREAKER_THRESHOLD:
        pause(scope, error or "unknown")


def pause(scope: str, reason: str) -> None:
    payload = {"reason": reason, "ts": int(time.time())}
    try:
        kv.put(f"pause:{scope}", payload, ttl_sec=BREAKER_COOLDOWN_SEC)
    except Exception:  # noqa: BLE001 - 写标记失败不能掩盖原始错误
        log.exception("写熔断标记失败 scope=%s", scope)
    _failures[scope] = 0
    log.error("熔断触发 scope=%s reason=%s", scope, reason)
    try:
        send_alert(
            f"[site-agent] 熔断：{scope}",
            f"<p>{scope} 连续失败 {BREAKER_THRESHOLD} 次，已暂停 "
            f"{BREAKER_COOLDOWN_SEC // 60} 分钟。</p><pre>{reason}</pre>",
        )
    except Exception:  # noqa: BLE001
        log.exception("熔断告警发送失败 scope=%s", scope)


def clear(scope: str) -> None:
    try:
        kv.delete(f"pause:{scope}")
    except Exception:  # noqa: BLE001
        log.exception("清除熔断标记失败 scope=%s", scope)
    _failures[scope] = 0
