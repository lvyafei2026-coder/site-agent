# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""distribution 域适配器：外链渠道的发现与投稿（消费方 tools/distribution_tools.py）。

reddit.py  公共 JSON 只读搜索，永不发帖（设计红线 §1.4）。
devto.py   官方 API：list_articles 只读列热门 / publish 发文（不可逆）。
"""

from . import devto, reddit

__all__ = ["devto", "reddit"]
