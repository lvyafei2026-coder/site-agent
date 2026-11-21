# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""全仓枚举真源：凡是「有限取值 + 业务含义」的字段，一律在此定义，禁止散落魔法值。

★2026-11-17（用户定）：以前 status="doing"、intent="general"、kind="ledger" 这类
  字符串散落在各模块，代码分析工具无法知道取值集合与含义。本模块把 site-agent
  所有枚举族集中为 ``StrEnum``：

  - 每个枚举类 = 一个字段的取值集合；每个成员 = 一个合法值，行内注释说明含义。
  - ``StrEnum`` 是 ``str`` 子类：与现有字符串比较、JSON 序列化、D1 参数绑定、
    Pydantic 字段全部兼容，替换不会改变任何线上行为（见 test_enums.py）。
  - **零项目内依赖**（只 import 标准库 enum）：tools/base.py、adapters/sql.py
    这两个叶子模块也能安全引用（守卫测试显式放行本模块）。

用法：::

    from src.app.enums import CandidateStatus, Intent

    snapshot(status=CandidateStatus.DOING)   # 等价于 status="doing"
    if intent == Intent.GENERAL: ...         # 等价于 intent == "general"

新增枚举值 = 在此加一个成员 + 注释含义；不要再在业务代码里写裸字符串。
"""

from __future__ import annotations

from enum import StrEnum


# ---------------------------------------------------------------------------
# 一、Agent 工作流
# ---------------------------------------------------------------------------
class Intent(StrEnum):
    """一轮运营的意图。llm 负责分类、tool_service 负责映射到工具集。"""

    SELECT_PAGES = "select_pages"          # 选题：捞候选词 + 站点清单 + 历史复盘
    PLAN_PROMO = "plan_promo"              # 推广排期：最新排期 + 站点清单 + 待消费事件
    FIND_BACKLINKS = "find_backlinks"      # 找外链：外链台账 + Reddit + DEV 文章
    REVIEW_METRICS = "review_metrics"      # 复盘：站点点击/GSC/页面清单，再调 LLM 分析
    ANALYZE_HOTWORDS = "analyze_hotwords"  # 热词逐词分析并回写状态（走独立入口 /hotwords，非工具）
    GENERAL = "general"                    # 通用兜底；无 LLM key 或非法意图都回退到它


class WorkflowStatus(StrEnum):
    """WorkflowResult 的最终状态：由 validation.is_valid 决定。"""

    SUCCESS = "success"  # 校验通过
    FAILED = "failed"    # 校验未通过（含工具全失败）


class GraphNode(StrEnum):
    """两条线的 LangGraph 节点名。用动词前缀，避免与 State 字段名同名（0.3.x 会报错）。"""

    # 常规线主图（graph/normal_graph.py）：分类 → 取数 → 校验 → 报告 / 错误
    INTENT = "classify_intent"      # 意图分类
    PROCESSING = "run_processing"   # 取数 / 调工具
    VALIDATION = "validate_result"  # 校验
    OUTPUT = "build_output"         # 生成报告
    ERROR = "handle_error"          # 终态错误处理
    # 热词循环图（graph/hotword_graph.py）：取一词 → 逐词子图 → 回环 / 汇总
    FETCH_ONE = "fetch_one"         # 问 tool 要下一个候选词
    ANALYZE_WORD = "analyze_word"   # 逐词子图整体（按本轮名单连线，一个词一次调用）
    SUMMARY = "build_summary"       # 终态汇总（与常规线 build_output 对称）

    # 逐词子图内的步骤节点：LLM 之前补证据，LLM 之后做后置动作，最后落账。
    # 「本轮走哪几步」就是 hotword_graph 里的连线，名单 → 节点名的表也在那儿。
    GATHER_SERP = "gather_serp"     # 补 SERP 辨形证据（fetch_serp 工具）
    GATHER_TREND = "gather_trend"   # 补 Google 趋势证据（fetch_trend 工具，只落库不进提示词）
    LLM_ANALYZE = "llm_analyze"     # 素材进提示词 → AI 结论 → 硬证据压结论
    WRITE_RESULT = "write_result"   # 结论回写（write_hotword_result 工具）
    COLLECT_WORD = "collect_word"   # 该词结果落进 results + 轮次计数


class RouteTag(StrEnum):
    """节点路由标签：edges.py 依此决定下一条边（两条线共用同一套写法）。"""

    RETRY = "retry"          # 校验没过但还能重试 → 回 processing
    COMPLETE = "complete"    # 校验通过 → output
    ERROR = "error"          # 终态失败 → error 节点
    VALIDATE = "validate"    # processing 无硬失败 → 进 validation
    LOOP = "loop"            # 热词还有词且未达上限 → 回 fetch_one
    SUMMARIZE = "summarize"  # 热词取尽 / 达上限 / KV 失败 → build_summary
    ANALYZE = "analyze"      # 取到词了 → 进逐词子图
    NEXT = "next"            # 逐词子图：LLM 出了结论 → 继续走后置动作
    COLLECT = "collect"      # 逐词子图：LLM 失败 / 没有后置动作 → 直接落账


class TraceName(StrEnum):
    """两条线运行入口的 LangSmith 根 span 名（在 @observed 声明处引用）。"""

    WORKFLOW_RUN = "site-agent:workflow-run"  # 常规线一轮（POST /run）
    HOTWORD_RUN = "site-agent:hotword-run"    # 热词线一轮（POST /hotwords）


class ToolKind(StrEnum):
    """工具副作用分级。写在 RuntimeTool.kind，决定编排时的信任级别。"""

    READ = "read"      # 只读，无副作用（缺省）
    LEDGER = "ledger"  # 写台账 / KV / 本地文件，可重复执行（幂等）
    WRITE = "write"    # 不可逆的外部动作（git push / 空提交重推构建 / 发 DEV 文章 / 发通知）


class LlmThinking(StrEnum):
    """DeepSeek 思考模式开关（LLM_THINKING 环境变量）。"""

    AUTO = "auto"  # 默认：仅当 base_url 指向 DeepSeek 时关闭思考
    OFF = "off"    # 强制关闭思考
    ON = "on"      # 保留思考（需自行调大 max_tokens）


class MemoryBackend(StrEnum):
    """LangGraph checkpointer 后端（MEMORY_BACKEND 环境变量）。"""

    MEMORY = "memory"  # 进程内，重启即丢（本地 / 测试默认）
    SQLITE = "sqlite"  # 落盘，配合 Render 的 /data 持久卷


# ---------------------------------------------------------------------------
# 二、候选词 / 热词整理（与 console / radar 共享 KV 口径）
# ---------------------------------------------------------------------------
class CandidateStatus(StrEnum):
    """候选池（KV candidates）过滤状态：get_hotwords 的 status 参数。

    ★与 CurationStatus 的关系：两者是**同一个「词的跟进进展」在两个存储阶段的投影**，
    不是重复定义——candidates 表由 radar 每轮重建（原始候选快照，还没进整理流程，
    只有 doing / live 两种过滤态有意义），saved 表才是完整生命周期（CurationStatus）。
    不要合并成一个宽枚举：合了之后 candidates 过滤会出现永远匹配不到的
    saved/todo/wip/parked，saved 表也会出现无意义的 doing。
    """

    DOING = "doing"  # 未跟进 / 待处理；老条目无 status 字段时按 doing 处理，不能丢
    LIVE = "live"    # 已上线成站，过滤掉（与 CurationStatus.LIVE 同义）


class CurationStatus(StrEnum):
    """收藏表（KV saved）的跟进状态，人工按钮循环切换。与 console/src/store.js 一致。

    ★与 CandidateStatus 的关系：saved 表接管 candidates 池里判定「可以做」的词之后
    的完整生命周期（见 CandidateStatus docstring 的投影说明）。
    """

    SAVED = "saved"    # 先收藏
    TODO = "todo"      # 待开发：Agent 判定「可以做」后自动置入，等人工开工
    WIP = "wip"        # 开发中
    LIVE = "live"      # 已上线
    PARKED = "parked"  # 暂时搁置：已关注但暂不投入


class RejectSource(StrEnum):
    """「不做」标记（KV rejected）的来源。人工优先：AI 重复判定不改人工原因。"""

    AI = "ai"        # site-agent 分析后判「不做」
    MANUAL = "manual"  # 人工标记


class HintName(StrEnum):
    """常青信号标签（第 8 节信号标签 / aihints 白名单），最多取 3 个。"""

    TOOL_QUERY = "工具查询"      # 工具检索类需求
    PRICE_SPEC = "价格规格"      # 价格 / 规格查询
    SCHEDULE_DATE = "日程日期"   # 日程 / 日期类
    ELIGIBILITY = "资格政策"     # 资格 / 政策类
    HOW_TO = "用法教程"          # 用法 / 教程类
    COMPARISON = "对比评测"      # 对比 / 评测类


class Verdict(StrEnum):
    """热词 AI 结论。空串代表「解析不出结论」（不是枚举成员，保持原语义）。"""

    DO = "做"          # 判定可做 → 写 saved(status=todo)
    WATCH = "观望"     # 暂不写状态，只留正文与信号标签
    SKIP = "不做"      # 写 rejected(source=ai)
    UNDECIDED = "未定"  # 统计展示占位：正文里解析不出结论时计入此类


class AnalysisPhase(StrEnum):
    """单词分析写入账本（_WriteLog.phase）的阶段，用于定位写到哪一步失败。"""

    SERP = "serp"        # 抓 SERP 竞争度
    TREND = "trend"      # 抓 30 天趋势
    ANALYSIS = "analysis"  # LLM 分析（默认）


class SearchTimeframe(StrEnum):
    """Reddit 搜索时间窗（外部 API 参数，单一合法值）。"""

    WEEK = "week"  # 近一周


# ---------------------------------------------------------------------------
# 三、台账 / D1 表字段口径
# ---------------------------------------------------------------------------
class EventStatus(StrEnum):
    """agent_events.status：事件消费状态。"""

    PENDING = "pending"    # 新事件待消费
    CONSUMED = "consumed"  # 已消费


class EventKind(StrEnum):
    """agent_events.kind：事件类型，get_pending_events 按此过滤。"""

    HOTWORDS_READY = "hotwords-ready"      # 新热词就绪
    PAGES_DEPLOYED = "pages-deployed"      # 新页上线
    PROMO_SCHEDULED = "promo-scheduled"    # 排期完成
    BACKLINKS_FOUND = "backlinks-found"    # 发现外链
    METRICS_READY = "metrics-ready"        # 指标就绪
    REFLECTION = "reflection"              # 复盘
    INDEXING_QUEUED = "indexing-queued"    # 待收录（publishing_tools 写入）


class ReportKind(StrEnum):
    """agent_reports.kind：报告 / 排期类型，唯一键 (report_date, kind)。"""

    DAILY = "daily"  # 日报（默认）
    WEEKLY = "weekly"  # 周报
    PLAN = "plan"      # 推广排期


class PlanStatus(StrEnum):
    """推广排期写入结果（write_promo_slot 的 result.status）。"""

    SCHEDULED = "scheduled"  # 追加成功
    SKIPPED = "skipped"      # 同日同渠道已存在，跳过


class PageStatus(StrEnum):
    """agent_pages.status：内容页生命周期。"""

    GENERATED = "generated"  # 已生成（默认）
    DEPLOYED = "deployed"    # 已部署
    INDEXED = "indexed"      # 已收录
    VERIFIED = "verified"    # 已验活
    FAILED = "failed"        # 失败（all 查询排除此项）


class BacklinkStatus(StrEnum):
    """agent_backlinks.status：外链台账状态。"""

    DRAFT = "draft"        # 草稿（默认）
    APPROVED = "approved"  # 已批准
    PUBLISHED = "published"  # 已发布
    DEAD = "dead"          # 失效（台账查询过滤掉）


class BacklinkChannel(StrEnum):
    """agent_backlinks.channel：外链来源渠道。"""

    DEVTO = "devto"        # DEV Community
    REDDIT = "reddit"      # Reddit
    DIRECTORY = "directory"  # 目录站


class PageQueryMode(StrEnum):
    """页面清单查询模式（get_page_inventory 的 mode）。"""

    ALL = "all"        # 全部去重（排除 failed）
    RECENT = "recent"  # 最近上线
    NEW = "new"        # 近 30 天新页（默认）
    REVIEW = "review"  # 上线满 7 天待复盘


class LedgerStrategy(StrEnum):
    """台账写入策略（LedgerSpec.strategy）。"""

    UPSERT = "upsert"  # 冲突即覆盖（默认）
    IGNORE = "ignore"  # 冲突即丢弃（重复通知不把 consumed 改回 pending）
    LOOKUP = "lookup"  # 先 SELECT 再决定 UPDATE / INSERT（给老表补登）


class TrackEvent(StrEnum):
    """埋点事件类型（tool-proxy 写入，agent 的 click_stats / stats 只读镜像）。"""

    PAGE_VIEW = "page_view"  # 页面浏览
    CLICK = "click"          # 按钮点击
    OUTBOUND = "outbound"    # 外链点击
    SUBMIT = "submit"        # 表单提交
    AUTH = "auth"            # Google 轻登录（仅服务端写入）


class SerpVerdict(StrEnum):
    """SERP 抓取结果判定（serp_verdict），供红线判断与诊断。"""

    ITEMS = "items"          # 真结果
    EMPTY = "empty"          # 真空结果
    CHALLENGE = "challenge"  # 202 挑战页 / 解析为空
    ERROR = "error"          # 其他 HTTP 失败


class DevtoStatus(StrEnum):
    """DEV Community 发布适配器的返回状态。"""

    BLOCKED = "blocked"    # 未配 DEVTO_API_KEY
    FAILED = "failed"      # 请求异常或非 200/201
    PUBLISHED = "published"  # 发布成功


class SerpEngine(StrEnum):
    """SERP 引擎标识（旧值兼容，只有两种）。"""

    DUCKDUCKGO = "duckduckgo"
    DUCKDUCKGO_LITE = "duckduckgo-lite"


class PromptTask(StrEnum):
    """D1 prompts 表的 task 分类；*_user 是同一任务的 human 模板。"""

    INTENT = "intent"
    INTENT_USER = "intent_user"
    EXTRACTION = "extraction"
    EXTRACTION_USER = "extraction_user"
    HOTWORDS = "hotwords"
    HOTWORDS_USER = "hotwords_user"
    REVIEW_METRICS = "review_metrics"
