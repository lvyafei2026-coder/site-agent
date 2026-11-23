# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""从自由文本里捞 JSON：剥 ```json 围栏、在散文中找第一个对象。

与业务无关——任何「模型或外部服务该返回 JSON、实际返回了带说明文字的文本」的场景都能用。
调用方拿到 None 后怎么降级是它自己的事（常规线在 src/app/llm/completion.py）。
"""

from __future__ import annotations

import json
import re


def strip_fences(text: str) -> str:
    """剥掉模型偶尔包在 JSON 外的 ```json ... ``` 围栏。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
        s = s.strip()
    return s


def parse_json(text: str) -> dict | None:
    """把文本解析成 dict；容忍两种情况：
    1) 被 ```json 围栏包裹；2) 散文里夹着一段 JSON（抽第一个 {…} 对象）。

    OpenAI 兼容服务偶尔忽略 response_format，返回散文/带说明文字而非纯 JSON；
    这里尽量「捞」出 JSON 对象，捞不到返回 None（上层走确定性回退）。
    """
    s = strip_fences(text or "").strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        mobj = re.search(r"\{.*\}", s, re.DOTALL)
        if not mobj:
            return None
        try:
            data = json.loads(mobj.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None
