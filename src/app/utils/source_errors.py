# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""外部数据源不可用（连不上 / 被限流封禁 / 凭据失效）——抓取层的通用失败契约。

为什么单独一个异常类型：抓外部源的失败分两种，处理方式完全不同。
  ① **源级不可用**（DNS/连接超时、429、202 挑战页占满 4 发）：不是某个词的问题，
     下一个词照样失败。一轮 100 个词 × 4 发 × 20 秒全打在超时上（实测一条词就要 90 秒），
     既烧时间又加重封禁。
  ② **单条问题**（这个词没结果、CSV 解析空）：降级即可，不影响其它条目。

★2026-11-13 改：源级不可用**不再中止整轮**。抛出后（现在的使用方是热词线的
  scrape_serp / scrape_trend，新源同理），对应的取数节点把这条目的这一步降级成 notice，
  并把步骤名记进本轮降级状态（DegradeState.disabled）让后续条目跳过这一步；
  LLM 与后置动作照常进行（一词一结果，互不影响）。
★本异常在横切层，所以工具层能直接认它：tools/base.py 的 is_retryable_error
  按 isinstance 判成「网络级、值得重试」（见 test_architecture 的 base 叶子守卫）。
"""

from __future__ import annotations

from typing import Any, Optional


class SourceUnavailable(RuntimeError):
    """数据源整体不可用；调用方应把本批的这一步降级，只让当前词跳过取数。"""

    def __init__(self, source: str, message: str, attempts: Optional[list[dict[str, Any]]] = None):
        super().__init__(message)
        self.source = source
        self.attempts = attempts or []

    def brief(self, limit: int = 160) -> str:
        """一行诊断：`duckduckgo 不可用：连接超时（4 发全失败）`。"""
        return f"{self.source} 不可用：{str(self)[:limit]}"
