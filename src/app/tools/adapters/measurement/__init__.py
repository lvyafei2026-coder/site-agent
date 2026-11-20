# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""measurement 域适配器：Search Console 取数（消费方 tools/measurement_tools.py）。

埋点聚合走 ../cloudflare/database.py，本包只放外部取数。
重依赖（google-api-python-client）在模块内延迟导入，导入本包不需要凭据。
"""

from . import gsc

__all__ = ["gsc"]
