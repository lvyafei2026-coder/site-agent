# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""D1 方言的唯一出口：固定语句、查询意图、台账契约、动态语句构造。

分三层，互不混杂：
    Sql          列名与条件都写死的语句，参数只有时间窗 / limit
    PAGE_QUERIES 查询意图 -> (SQL, 时间窗天数)，时间窗是业务口径不是 SQL
    LedgerSpec   台账表 -> 白名单列 / 唯一键 / 写入策略
    SqlBuilder   由 LedgerSpec 拼 INSERT / UPDATE / UPSERT（表名列名不可外部传入）

取数与参数组装在各域 <Domain>Ops，SQL 字符串只出现在本文件。
"""

from __future__ import annotations

from dataclasses import dataclass


class Sql:
    """固定语句。events 表真实 schema：ts(ms) / site / event(page_view|...) / path / sid。"""

    DAILY_STATS = """
SELECT site,
       CAST(ts / 86400000 AS INTEGER) * 86400000 AS day,
       COUNT(*) AS pv,
       COUNT(DISTINCT sid) AS uv
FROM events
WHERE ts >= ? AND event = 'page_view'
GROUP BY site, day
ORDER BY day DESC, pv DESC
LIMIT 500
"""

    TOP_PAGES = """
SELECT site, path, COUNT(*) AS pv, COUNT(DISTINCT sid) AS uv
FROM events
WHERE ts >= ? AND event = 'page_view'
GROUP BY site, path
ORDER BY pv DESC
LIMIT 50
"""

    LATEST_PLAN = (
        "SELECT actions FROM agent_reports WHERE kind = 'plan' "
        "ORDER BY id DESC LIMIT 1"
    )
    BACKLINK_LEDGER = (
        "SELECT channel, source_url, target_url, status FROM agent_backlinks "
        "WHERE status != 'dead' LIMIT ?"
    )
    REFLECTIONS = (
        "SELECT slot, action_key, reason, detail, created_at FROM agent_reflections "
        "ORDER BY id DESC LIMIT ?"
    )
    PENDING_EVENTS = (
        "SELECT * FROM agent_events WHERE status = 'pending' "
        "ORDER BY id ASC LIMIT ?"
    )
    CONSUME_EVENT = (
        "UPDATE agent_events SET status = 'consumed', consumed_at = ?, thread_id = ? "
        "WHERE event_key = ?"
    )


@dataclass(frozen=True, slots=True)
class PageQuery:
    """一条页面清单查询：SQL 决定取哪些列，window_days 决定回溯多久（0 = 不限）。"""

    sql: str
    window_days: int = 0


PAGE_QUERIES: dict[str, PageQuery] = {
    "all": PageQuery(
        "SELECT site, slug, keyword, status FROM agent_pages WHERE status != 'failed'"
    ),
    "recent": PageQuery(
        "SELECT site, slug, keyword, created_at FROM agent_pages "
        "WHERE status IN ('deployed','indexed','verified') "
        "ORDER BY created_at DESC"
    ),
    "new": PageQuery(
        "SELECT site, slug, keyword, created_at FROM agent_pages "
        "WHERE status IN ('deployed','indexed','verified') "
        "AND created_at >= ? ORDER BY created_at DESC",
        window_days=30,
    ),
    "review": PageQuery(
        "SELECT site, slug, keyword, status, created_at, gsc_clicks "
        "FROM agent_pages WHERE created_at <= ? ORDER BY created_at DESC",
        window_days=7,
    ),
}


@dataclass(frozen=True, slots=True)
class LedgerSpec:
    """一张台账表的写入契约。

    cols     允许写入的列（白名单，挡住调用方多传的键）
    unique   唯一键，upsert 的 ON CONFLICT 目标
    strategy upsert  冲突即覆盖
             ignore  冲突即丢弃（重复通知不把已 consumed 的事件改回 pending）
             lookup  先 SELECT 再决定 UPDATE / INSERT（给 0001 老表补登）
    """

    cols: tuple[str, ...]
    unique: tuple[str, ...]
    strategy: str = "upsert"


LEDGER_TABLES: dict[str, LedgerSpec] = {
    "agent_pages": LedgerSpec(
        cols=("site", "keyword", "slug", "status", "created_at", "verified_at",
              "gsc_clicks", "gsc_impressions", "avg_position"),
        unique=("site", "slug"),
    ),
    "agent_reports": LedgerSpec(
        cols=("report_date", "kind", "summary", "anomalies", "actions", "created_at"),
        unique=("report_date", "kind"),
    ),
    "agent_backlinks": LedgerSpec(
        cols=("channel", "target_url", "source_url", "anchor_text", "status",
              "score", "created_at", "checked_at", "event_key"),
        unique=("event_key",),
        strategy="lookup",
    ),
    "agent_events": LedgerSpec(
        cols=("event_key", "kind", "payload", "status", "created_at",
              "consumed_at", "thread_id"),
        unique=("event_key",),
        strategy="ignore",
    ),
    "agent_reflections": LedgerSpec(
        cols=("event_key", "slot", "action_key", "reason", "detail", "created_at"),
        unique=("event_key",),
    ),
    "agent_tool_calls": LedgerSpec(
        cols=("event_key", "tool", "kind", "thread_id", "slot", "args",
              "ok", "detail", "created_at"),
        unique=("event_key",),
    ),
    "agent_idempotency": LedgerSpec(
        cols=("idem_key", "tool", "result", "created_at"),
        unique=("idem_key",),
    ),
}


class SqlBuilder:
    """动态语句：表名与列名只能来自 LedgerSpec，值一律走 ? 占位。"""

    __slots__ = ()

    @staticmethod
    def _marks(items: tuple[str, ...] | list[str]) -> str:
        return ", ".join("?" for _ in items)

    @staticmethod
    def _where(cols: tuple[str, ...]) -> str:
        return " AND ".join(f"{c} = ?" for c in cols)

    @classmethod
    def select_by_unique(cls, table: str, unique: tuple[str, ...]) -> str:
        return f"SELECT rowid FROM {table} WHERE {cls._where(unique)}"

    @classmethod
    def update_by_unique(cls, table: str, updates: list[str],
                         unique: tuple[str, ...]) -> str:
        sets = ", ".join(f"{c} = ?" for c in updates)
        return f"UPDATE {table} SET {sets} WHERE {cls._where(unique)}"

    @classmethod
    def insert(cls, table: str, cols: list[str] | tuple[str, ...]) -> str:
        return (f"INSERT INTO {table} ({', '.join(cols)}) "
                f"VALUES ({cls._marks(cols)})")

    @classmethod
    def write(cls, table: str, cols: list[str], spec: LedgerSpec) -> str:
        """按策略生成「插入或冲突时更新 / 忽略」语句。"""
        head = cls.insert(table, cols)
        conflict = f"ON CONFLICT({', '.join(spec.unique)})"
        if spec.strategy == "ignore":
            return f"{head} {conflict} DO NOTHING"
        updates = [c for c in cols if c not in spec.unique]
        if not updates:  # 全列都是唯一键，没有可覆盖的字段
            return f"{head} {conflict} DO UPDATE SET {spec.unique[0]} = excluded.{spec.unique[0]}"
        sets = ", ".join(f"{c} = excluded.{c}" for c in updates)
        return f"{head} {conflict} DO UPDATE SET {sets}"

    @classmethod
    def pending_events(cls, kinds: tuple[str, ...] | list[str]) -> str:
        marks = cls._marks(kinds)
        return (f"SELECT * FROM agent_events WHERE status = 'pending' "
                f"AND kind IN ({marks}) ORDER BY id ASC LIMIT ?")
