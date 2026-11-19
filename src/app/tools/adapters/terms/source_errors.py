# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""外部数据源不可用（连不上 / 被限流封禁 / 凭据失效）。

为什么单独一个异常类型：SERP 与 Trends 的失败分两种，处理方式完全不同。
  ① **源级不可用**（DNS/连接超时、429、202 挑战页占满 4 发）：不是某个词的问题，
     下一个词照样失败。20 个词 × 4 发 × 20 秒全打在超时上（实测一条词就要 90 秒），
     既烧时间又加重封禁。
  ② **单词问题**（这个词没结果、CSV 解析空）：降级即可，不影响其它词。

★2026-11-13 改：源级不可用**不再中止整批**。scrape_serp / scrape_trend 抛出后，
  hotwords._analyze_one 把该词的这一步降级成 notice，并置批次级 serp_off / trends_off
  让后续词跳过取数；LLM 与状态回写照常进行（完成一个结束一个、互不影响）。
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
