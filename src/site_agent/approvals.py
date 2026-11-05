# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""审批回复的 IMAP 轮询。

为什么用 IMAP 而不是 HTTP 回调：Render Background Worker 没有公网入口，
主动出站收信是零基础设施的做法（Gmail/企业邮箱都支持 IMAP + 应用专用密码）。
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from email.header import decode_header, make_header
from typing import Callable

from .config import env

log = logging.getLogger(__name__)

TID_RE = re.compile(r"\[site-agent\][^:：]*[:：]\s*(\S+)")
REPLY_MARKERS = re.compile(r"\n(?:>|On .+wrote:|-{2,}\s*Original)", re.IGNORECASE)
MAX_PER_RUN = 10


def _decode(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _plain_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True) or b""
                return raw.decode(part.get_content_charset() or "utf-8", "ignore")
        return ""
    raw = msg.get_payload(decode=True) or b""
    return raw.decode(msg.get_content_charset() or "utf-8", "ignore")


def _decision(body: str) -> bool | None:
    """只看回复正文第一段，避免把引用的原文误判。"""
    head = REPLY_MARKERS.split(body, maxsplit=1)[0].lower()
    if re.search(r"\byes\b", head):
        return True
    if re.search(r"\bno\b", head):
        return False
    return None


def poll_once(resume_fn: Callable[[str, bool], dict]) -> int:
    host = env("IMAP_HOST")
    user = env("IMAP_USER")
    password = env("IMAP_PASS")
    if not (host and user and password):
        return 0

    handled = 0
    conn: imaplib.IMAP4_SSL | None = None
    try:
        conn = imaplib.IMAP4_SSL(host, timeout=30)
        conn.login(user, password)
        conn.select("INBOX")
        typ, data = conn.search(None, "UNSEEN", "SUBJECT", '"site-agent"')
        if typ != "OK" or not data or not data[0]:
            return 0

        for num in data[0].split()[-MAX_PER_RUN:]:
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode(msg.get("Subject"))
            match = TID_RE.search(subject)
            if not match:
                continue
            thread_id = match.group(1).strip()
            approved = _decision(_plain_body(msg))
            if approved is None:
                log.info("审批邮件未给出明确结论，保持未读：%s", subject)
                continue
            try:
                resume_fn(thread_id, approved)
                handled += 1
            except Exception:  # noqa: BLE001 - 单封失败不影响其他邮件
                log.exception("恢复任务失败 %s", thread_id)
            conn.store(num, "+FLAGS", "\\Seen")
    except (imaplib.IMAP4.error, OSError) as exc:
        log.warning("IMAP 轮询失败：%s", exc)
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
    return handled
