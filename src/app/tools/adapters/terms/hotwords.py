# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""热词子系统的**公共 API 门面**：聚合对外契约与稳定测试入口，不承载逻辑、不做运行时跳转。

2026-11-12 去接口化：本模块不调用 site-console 的 /api/hotwords 与
google-trend-radar 的 /api/*，直接读写三方共用的共享 KV（见 ../cloudflare/kv.py）。

★2026-11-19 结构收敛：
    - 生产契约只有 run_batch（hotwords_tools / main）与 backfill_states（api/routes）。
    - 门面 re-export 的是**公共 API**（判据函数、KV 键名、trace 常量、抓取模块对象、
      LLM 诊断），供测试与外部按 hotwords.<name> 引用；它们是稳定的只读出口，
      不是「内部实现的镜子」。真正制造环的运行时跳转已全部删除：
        · worker._resolve_llm（门面取 _llm）→ worker 直接 hotword_llm._llm
        · hotword_loop.get_hotword_loop（图 shim）→ batch 直连 builder 的图
        · hotword_loop._batch_symbols（回取 batch）→ 共享符号下沉 hotword_contract
    - 图内部符号（HotwordLoopState / fetch_one / analyze_one / should_continue）不再
      从门面导出——它们是 builder 装配用的内部件，测试走 hotword_loop 真身。

分层（依赖单向向下，无环）：
    batch → builder → loop → worker → fetch / criteria / llm / kv_io / contract
★发给 LLM 的 system / user 模板唯一真源 = radar-rules D1 的 prompts 表（由 /rules/prompts
  页面维护，本地 versions/*.txt 已删），判据层只「拼数据」。
"""

from __future__ import annotations

# ---- 生产契约 ----
from .hotword_batch import run_batch  # noqa: F401
from .hotword_backfill import backfill_states  # noqa: F401

# ---- 候选筛选规则 + trace 常量（公共 API）----
from .hotword_contract import (  # noqa: F401
    _select_pending, BATCH_OK_MIN_RATE, BATCH_TRACE_NAME, WORD_TRACE_PREFIX,
)

# ---- 外部抓取模块对象（测试按 hw.scrape_serp / hw.scrape_trend patch 共享对象）----
from . import scrape_serp, scrape_trend  # noqa: F401
from .source_errors import SourceUnavailable  # noqa: F401
from .hotword_fetch import (  # noqa: F401
    SERP_GAP_SEC, SERP_TTL, TREND_TTL, _throttled_serp, _trend,
)

# ---- 判据层公共 API ----
from .hotword_criteria import (  # noqa: F401
    EXAMPLE_LIMIT, _prompt_pair, pick_examples, examples_line,
    HINT_NAMES, WORK_TITLE_RX, build_messages,
    serp_brief, work_signal, verdict_of, redline_of, signals_of, decide,
)

# ---- LLM 层公共 API ----
from .hotword_llm import (  # noqa: F401
    MAX_TOKENS, LLMEmptyContent, _field, _thinking_kwargs,
    _empty_content_diag, _llm,
)

# ---- KV 写账本 + 键名契约（公共 API）----
from .hotword_kv_io import _WriteLog, _kv_list, _kv_dict  # noqa: F401
from src.app.tools.adapters.cloudflare.kv import (  # noqa: F401
    term_key,
    AI_CACHE_PREFIX, SERP_PREFIX, TREND_PREFIX,
    SAVED_KEY, REJECTED_KEY, TAGS_KEY, COMPETITION_KEY, INTERESTS_KEY, HINTS_KEY,
)

# ---- 单词编排公共 API ----
from .hotword_worker import (  # noqa: F401
    TOOL_TRACE_PREFIX, _tool_step, _BatchAbort, _finalize, _analyze_one,
    _word_outputs, _word_error,
)
