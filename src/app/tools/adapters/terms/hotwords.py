# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""terms 适配器的只读门面：聚合本层的判据 / IO / 常量 / 抓取模块，不承载逻辑。

2026-11-20 分层归位后，本门面**只向下聚合 adapters/terms 叶子层符号**：
    - 热词图 run_hotwords → graph/hotword_graph.py
    - 回填 backfill_states → services/backfill_service.py
    - 单词编排 _analyze_one / _DegradeState → services/hotword_service.py
这些上层入口不再从本门面 re-export（那会让 adapters 反向依赖 graph/services）。
调用方与测试按新路径引用即可。

2026-11-12 去接口化：agent 不调 console 的 /api/hotwords，直接读写三方共用的共享 KV。
★发给 LLM 的 system / user 模板唯一真源 = radar-rules D1 的 prompts 表。
"""

from __future__ import annotations

# ---- 候选筛选规则 + trace 常量（本层契约叶子）----
from .hotword_contract import (  # noqa: F401
    select_pending, OK_MIN_RATE, RUN_TRACE_NAME, WORD_TRACE_PREFIX,
)

# ---- 外部抓取模块对象 ----
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

# ---- KV 写账本 + 键名契约（本层公共 API）----
from .hotword_kv_io import _WriteLog, _kv_list, _kv_dict  # noqa: F401
from src.app.tools.adapters.cloudflare.kv import (  # noqa: F401
    term_key,
    AI_CACHE_PREFIX, SERP_PREFIX, TREND_PREFIX,
    SAVED_KEY, REJECTED_KEY, TAGS_KEY, COMPETITION_KEY, INTERESTS_KEY, HINTS_KEY,
)
