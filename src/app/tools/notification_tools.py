# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""notification 域：给运营者的对外触达（定义 + 实现同文件）。

发信与 ledger 域的记账不是一个性质：台账写幂等、可重放，邮件发出去收不回。
所以它单独成域、单独 kind=write，默认工具集（INTENT_TOOLS）里永不出现，
只有请求显式点名才跑。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.app.enums import ToolKind
from src.app.tools.adapters.notification import email
from src.app.tools.base import RuntimeTool


class NotificationOps:
    """notification 域的实现：只碰 adapters/notification。"""

    def send_notice(self, subject, html):
        return {"sent": bool(email.send(subject, html))}


class SendNoticeTool(RuntimeTool):
    """给运营者发一封通知邮件（日报、告警、已批准草稿）。不可逆：经 Resend 真发信，发出去收不回。"""

    tool_name = "send_notice"
    method = "send_notice"
    kind = ToolKind.WRITE.value

    class Input(BaseModel):
        subject: str = Field(..., description="标题")
        html: str = Field(..., description="HTML 正文")
