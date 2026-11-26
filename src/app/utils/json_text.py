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


def _next_closer(text: str, start: int) -> str:
    """跳过空白看下一个非空字符，只在判断「两个闭合符是否写反」时用。"""
    for ch in text[start:]:
        if not ch.isspace():
            return ch
    return ""


def _scan_repair(text: str, drop_stray: bool) -> str | None:
    """补齐闭合符；drop_stray 决定「闭合符与栈顶对不上」时是否直接丢掉。

    写反和多写一个闭合符，扫描时看到的是同一个现象（``}"`` 紧跟着 ``]``），
    区别只在后面还有没有多余的收尾：多写的那个该丢，写反的那个丢不得。
    两种都试，能不能解析由 repair_json 统一判定。
    """
    out: list[str] = []
    stack: list[str] = []
    in_str = False
    esc = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch in "{[":
            stack.append(ch)
            out.append(ch)
            i += 1
            continue
        if ch in "}]":
            opener = "{" if ch == "}" else "["
            partner = "]" if ch == "}" else "}"
            if (drop_stray and stack and stack[-1] != opener
                    and _next_closer(text, i + 1) == partner):
                i += 1
                continue
            # 漏写闭合符的两种形态都补：数组里先出 }，或对象里先出 ]（FAQ 末条实测）。
            while stack and stack[-1] != opener:
                out.append("]" if stack.pop() == "[" else "}")
            if not stack:
                return None
            stack.pop()
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    if in_str:
        return None
    while stack:
        out.append("]" if stack.pop() == "[" else "}")
    return "".join(out)


def repair_json(text: str) -> str | None:
    """补齐模型漏写/错写的数组、对象闭合符（只动结构，不改内容）。

    线上实例（page_content / deepseek-flash），模型写长 JSON 时三种坏法都出现过：
    1) 漏写闭合符：小节正文数组末尾写成 ``"...already high."}``（少 ``]``），
       FAQ 末条写成 ``"...December."]``（少 ``}``）；
    2) 闭合符顺序颠倒：``"...good."}],`` 应为 ``"]}``；
    3) 颠倒且多写一个：``"...exposures."}]},`` 里开头的 ``}`` 是多的。
    先按「只补不删」修，修不出可解析结果再按「丢掉多余的闭合符」修；只返回能真正
    解析成 dict 的结果，都修不好返回 None，绝不把半成品交给上层。
    """
    s = str(text or "")
    for drop_stray in (False, True):
        candidate = _scan_repair(s, drop_stray)
        if candidate is not None and _loads_dict(candidate) is not None:
            return candidate
    return None


def _loads_dict(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_json(text: str) -> dict | None:
    """把文本解析成 dict；容忍三种情况：
    1) 被 ```json 围栏包裹；2) 散文里夹着一段 JSON（抽第一个 {…} 对象）；
    3) 结构上漏了闭合符（见 repair_json）。

    OpenAI 兼容服务偶尔忽略 response_format，返回散文/带说明文字而非纯 JSON；
    这里尽量「捞」出 JSON 对象，捞不到返回 None（上层走确定性回退）。
    """
    s = strip_fences(text or "").strip()
    data = _loads_dict(s)
    if data is not None:
        return data
    repaired = repair_json(s)
    if repaired is not None and repaired != s:
        data = _loads_dict(repaired)
        if data is not None:
            return data
    mobj = re.search(r"\{.*\}", s, re.DOTALL)
    if not mobj:
        return None
    return _loads_dict(mobj.group(0))


def unwrap_envelope(data: dict, depth: int = 3) -> dict:
    """剥掉 {"type": "text", "content": "<内层 JSON>"} 这类外壳。

    实测 OpenAI 兼容网关会把结构化答案再包一层，且 content 常常是二次编码
    （字符串里又是一段带转义的 JSON）。判定只看形态：顶层 content 是字符串
    且能解析成 dict 才算壳——业务数据里的 content 是正文文本，解析不成 dict，
    不会被误拆。最多剥 depth 层，剥不动就原样返回，由上层按其真实字段报告。
    """
    for _ in range(depth):
        inner = data.get("content")
        parsed = _as_dict(inner) if isinstance(inner, str) else None
        if parsed is None:
            return data
        data = parsed
    return data


def _as_dict(text: str) -> dict | None:
    """字符串 → dict；容忍二次编码（解析出来还是 JSON 字符串）。"""
    parsed = parse_json(text)
    if parsed is not None:
        return parsed
    try:
        inner = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parse_json(inner) if isinstance(inner, str) else None
