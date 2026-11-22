# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""notification 域适配器：对运营者的对外触达通道。

消费方：tools/notification_tools.py。

email.py  Resend 发信（日报 / 告警 / 结果摘要）。发出去收不回，
          所以 send_notice 是 write 类工具，只有请求显式点名才跑。
"""

from . import email

__all__ = ["email"]
