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
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel

# requests 是硬依赖，但 base 作为叶子模块不宜在顶层强绑。包一层 try，缺失时
# 网络型异常判定退化为只用标准库集合（极罕见，仅防导入崩溃）。
try:
    import requests
    _REQ_NETWORK_EXC = (
        requests.exceptions.Timeout,        # 含 ConnectTimeout / ReadTimeout
        requests.exceptions.ConnectionError,
    )
except Exception:  # pragma: no cover
    _REQ_NETWORK_EXC = ()

# 网络 / 连接层瞬态故障：值得重试。这些异常不代表代码或配置错误，重试一次（或等
# 下次请求）通常能恢复。注意 requests.HTTPError（4xx/5xx）不属于这里——4xx 是明确的
# 客户端错误（鉴权/参数），5xx 由调用方决定，默认不当瞬态网络故障处理。
_RETRYABLE_EXC_TYPES = _REQ_NETWORK_EXC + (
    socket.timeout,        # 含 socket.timeout 别名
    socket.gaierror,       # DNS 解析失败，网络级
    TimeoutError,          # 内置超时（requests 之外路径）
    ConnectionError,       # 内置，覆盖 ConnectionResetError 等
)


def is_retryable_error(exc: BaseException) -> bool:
    """判定异常是否「网络/连接层」瞬态故障（应走重试），而非明确的业务/配置失败。

    返回 True：requests 的 Timeout / ConnectTimeout / ReadTimeout / ConnectionError、
    内置 socket.timeout / socket.gaierror / TimeoutError / ConnectionError，以及本项目
    的 SourceUnavailable（数据源整体不可用，网络级，应等下次重试）。

    返回 False：鉴权 401/403、参数校验（pydantic ValidationError）、KeyError、
    ValueError、业务异常等——这些是「明确的非网络失败」，重试没用，应直接结束、
    等调用方下次请求再试。
    """
    if isinstance(exc, _RETRYABLE_EXC_TYPES):
        return True
    # SourceUnavailable 语义是「源挂了」，按名判等避免跨层 import
    if type(exc).__name__ == "SourceUnavailable":
        return True
    return False


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
    retryable: bool = False  # 失败时为 True = 网络/连接层瞬态故障（应重试）；False = 明确非网络失败


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
            # 把异常分类：网络/连接层瞬态故障 → retryable=True（走重试）；
            # 明确非网络失败（鉴权/参数/业务异常）→ retryable=False（不重试）。
            return ToolOutput(
                tool=self.name, ok=False,
                error=str(exc)[:300],
                retryable=is_retryable_error(exc),
            ).model_dump()
