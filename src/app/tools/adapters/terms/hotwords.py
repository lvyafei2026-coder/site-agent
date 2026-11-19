# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词分析门面：按层拆分后保持 ``hotwords.<name>`` 引用不变。

2026-11-12 去接口化：本模块**不调用** site-console 的 /api/hotwords 与
google-trend-radar 的 /api/*。它直接读写三方共用的共享 KV（Cloudflare KV REST API，
见 ../cloudflare/kv.py），数据链路只有「存储介质依赖」，没有「服务接口依赖」。

★2026-11-19 三层拆分（原单文件 686 行 → 门面 + 三层）：
    hotword_fetch.py    外部取数：SERP / Trends 读缓存 → 现抓 → 写回 + 全局节流。
    hotword_worker.py   单词编排：补数据 → LLM → 结论判定 → 回写状态（失败隔离到单词）。
    hotword_batch.py    批次编排：候选筛选 → 并发逐词 → 汇总 + 批次 trace 根。
  另有既存四层（2026-11-17 拆）：hotword_criteria（判据）/ hotword_llm（LLM 调用）/
  hotword_kv_io（写账本 + 容错读）/ hotword_backfill（状态回填）。
  下面把公开名字原样再导出——tests / runbook / api/routes.py 都按 hotwords.<name> 引用，
  门面名不变。依赖单向：batch → worker → fetch → kv_io，门面只做再导出。

★发给 LLM 的 system / user 模板唯一真源 = radar-rules D1 的 prompts 表（由 /rules/prompts
  页面维护，本地 versions/*.txt 已删），判据层只「拼数据」。
"""

from __future__ import annotations

# ---- 外部取数层（模块对象 + 取数辅助）----
from . import scrape_serp, scrape_trend  # noqa: F401  (测试按 hw.scrape_serp.patch)
from .source_errors import SourceUnavailable  # noqa: F401
from .hotword_fetch import (  # noqa: F401
    SERP_GAP_SEC, SERP_TTL, TREND_TTL, _throttled_serp, _trend,
)

# ---- 判据层 ----
from .hotword_criteria import (  # noqa: F401
    EXAMPLE_LIMIT, _prompt_pair, pick_examples, examples_line,
    HINT_NAMES, WORK_TITLE_RX, build_messages,
    serp_brief, work_signal, verdict_of, redline_of, signals_of, decide,
)

# ---- LLM 层 ----
from .hotword_llm import (  # noqa: F401
    MAX_TOKENS, LLMEmptyContent, _field, _thinking_kwargs,
    _empty_content_diag, _llm,
)

# ---- KV 写账本层 ----
from .hotword_kv_io import _WriteLog, _kv_list, _kv_dict  # noqa: F401

# ---- KV 键名契约（门面旧引用，供测试与外部按 hotwords.<name> 取）----
from src.app.tools.adapters.cloudflare.kv import (  # noqa: F401
    term_key,
    AI_CACHE_PREFIX, SERP_PREFIX, TREND_PREFIX,
    SAVED_KEY, REJECTED_KEY, TAGS_KEY, COMPETITION_KEY, INTERESTS_KEY, HINTS_KEY,
)

# ---- 单词编排层 ----
from .hotword_worker import (  # noqa: F401
    TOOL_TRACE_PREFIX, _tool_step, _BatchAbort, _finalize, _analyze_one,
    _word_outputs, _word_error,
)

# ---- 批次编排层 ----
from .hotword_batch import (  # noqa: F401
    BATCH_WORKERS, BATCH_OK_MIN_RATE, BATCH_TRACE_NAME, WORD_TRACE_PREFIX,
    _select_pending, _batch_outputs, _trace_run_info, run_batch, _run_batch,
)

# ---- 状态回填（api/routes.py 经 hotwords.backfill_states 引用）----
from .hotword_backfill import backfill_states  # noqa: F401
