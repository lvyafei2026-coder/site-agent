# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""通知：Resend 发信（日报 / 告警 / 人工审批请求）。所有发送失败都不阻塞主流程。"""

from __future__ import annotations

import logging

import requests

from ..config import env

log = logging.getLogger(__name__)

RESEND_API = "https://api.resend.com/emails"
FROM = "site-agent <hello@toolara.dev>"


def send(subject: str, html: str, to: str | None = None) -> bool:
    api_key = env("RESEND_API_KEY")
    recipient = to or env("ADMIN_EMAIL")
    if not api_key or not recipient:
        log.warning("未配置 RESEND_API_KEY / ADMIN_EMAIL，邮件跳过：%s", subject)
        return False
    try:
        resp = requests.post(
            RESEND_API,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": FROM, "to": [recipient], "subject": subject, "html": html},
            timeout=20,
        )
        if resp.status_code >= 300:
            log.warning("邮件发送失败 %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        log.warning("邮件发送异常：%s", exc)
        return False


def send_alert(subject: str, html: str) -> bool:
    return send(subject, html)


def send_approval_request(thread_id: str, action: dict) -> bool:
    """人审邮件：回复 yes 即执行（IMAP 轮询收信，无需公网入口）。"""
    summary = action.get("summary") or action.get("type", "")
    payload = action.get("payload", {})
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#5f6673'>{k}</td>"
        f"<td style='padding:4px 0'><code>{v}</code></td></tr>"
        for k, v in payload.items()
        if isinstance(v, (str, int, float))
    )
    html = f"""
    <div style="font:15px/1.7 -apple-system,sans-serif;color:#111827">
      <p><b>{summary}</b></p>
      <table style="border-collapse:collapse;font-size:14px">{rows}</table>
      <p style="margin-top:16px">任务：<code>{thread_id}</code></p>
      <p>回复本邮件并写 <b>yes</b> 执行，写 <b>no</b> 拒绝。未回复则保持挂起。</p>
    </div>
    """
    return send(f"[site-agent] 待确认：{thread_id}", html)
