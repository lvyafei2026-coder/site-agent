# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""工具契约。

26 个工具各自是 BaseTool 的子类，子类只声明 tool_name / method / Input，
作用说明写在类 docstring 里（description 直接取 docstring），实现在同一个
*_tools.py 的 <Domain>Ops 类里，外部 IO 只发生在 tools/adapters/ 内。

本模块是 tools 包的叶子：不 import 任何包内模块，也不持有全局状态。工具要用的
外部 IO 门面（RuntimeService）由装配方 registry.py 构造后作为参数注入，
所以 base → 任何域、域 → base 都成立，没有循环。
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel


def stable_key(*parts: Any) -> str:
    """跨进程稳定的幂等键（替代每次启动都变的 abs(hash(...))）。"""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class BaseTool(ABC):
    name: str
    description: str

    @abstractmethod
    def run(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


class ToolOutput(BaseModel):
    """所有工具统一返回体，节点与 API 只认这个形状。"""

    tool: str
    ok: bool = True
    result: Any = None
    error: str = ""


class RuntimeTool(BaseTool):
    """把 Input 参数转给注入的门面（ops）上的同名 method。

    子类只写三样：docstring（= 作用说明）、tool_name / method、嵌套 Input。
    kind 缺省 read；ledger / write 必须显式声明，方便一眼看出哪些会落库或不可逆。
    """

    tool_name: ClassVar[str] = ""
    method: ClassVar[str] = ""
    kind: ClassVar[str] = "read"
    tool_desc: ClassVar[str] = ""  # 留空即取类 docstring，一般不用写
    fixed: ClassVar[dict[str, Any]] = {}  # 补进调用的固定参数
    Input: ClassVar[type[BaseModel]]

    def __init__(self, ops: Any) -> None:
        super().__init__(name=self.tool_name,
                         description=self.tool_desc or (type(self).__doc__ or "").strip())
        self.ops = ops  # 外部 IO 的唯一入口，装配时注入（见 tools/registry.py）

    def run(self, **kwargs: Any) -> dict[str, Any]:
        try:
            payload = self.Input(**kwargs).model_dump(exclude_none=True)
            result = getattr(self.ops, self.method)(**{**payload, **self.fixed})
            return ToolOutput(tool=self.name, result=result).model_dump()
        except Exception as exc:  # noqa: BLE001 - 单工具失败不拖垮整轮
            return ToolOutput(tool=self.name, ok=False,
                              error=str(exc)[:300]).model_dump()
