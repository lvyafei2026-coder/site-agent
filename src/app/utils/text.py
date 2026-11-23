# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""文本截断：超长时截到上限并留尾巴，None / 非字符串都当空串处理。

两种尾巴各有用途，别再各处手写第三份：
    clip            固定省略号——给 trace / 日志展示，读者只要知道「后面还有」
    clip_with_count 带被砍掉的字符数——喂 LLM 前的预览，需要让人判断丢了多少内容
"""

from __future__ import annotations

from typing import Any


def clip(text: Any, limit: int, marker: str = "…") -> str:
    """截到 limit 个字符，超出时尾部接 marker。"""
    s = "" if text is None else text if isinstance(text, str) else str(text)
    return s if len(s) <= limit else s[:limit] + marker


def clip_with_count(text: Any, limit: int) -> str:
    """截到 limit 个字符，超出时尾部报出被砍掉的字符数。"""
    s = "" if text is None else text if isinstance(text, str) else str(text)
    return s if len(s) <= limit else s[:limit] + f"...<truncated {len(s) - limit} chars>"
