# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""通知：Resend 发信（日报 / 告警 / 结果摘要）。

发送失败只记日志并返回 False，不阻塞主流程；缺凭据时直接跳过。
"""

from __future__ import annotations

import logging

import requests

from src.app.config import CONTACT_EMAIL, env

log = logging.getLogger(__name__)

RESEND_API = "https://api.resend.com/emails"


def _from() -> str:
    return env("MAIL_FROM", f"site-agent <{CONTACT_EMAIL}>")


def send(subject: str, html: str, to: str | None = None) -> dict:
    """返回 {sent, detail}：detail 说明跳过/失败原因，供调用方回传给上游排查。"""
    api_key = env("RESEND_API_KEY")
    recipient = to or env("ADMIN_EMAIL")
    if not api_key or not recipient:
        missing = [k for k, v in (("RESEND_API_KEY", api_key), ("ADMIN_EMAIL", recipient)) if not v]
        log.warning("未配置 %s，邮件跳过：%s", "/".join(missing), subject)
        return {"sent": False, "detail": f"未配置 {'/'.join(missing)}"}
    try:
        resp = requests.post(
            RESEND_API,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": _from(), "to": [recipient], "subject": subject, "html": html},
            timeout=20,
        )
        if resp.status_code >= 300:
            log.warning("邮件发送失败 %s: %s", resp.status_code, resp.text[:200])
            return {"sent": False, "detail": f"Resend {resp.status_code}: {resp.text[:200]}"}
        return {"sent": True, "detail": ""}
    except requests.RequestException as exc:
        log.warning("邮件发送异常：%s", exc)
        return {"sent": False, "detail": str(exc)}
