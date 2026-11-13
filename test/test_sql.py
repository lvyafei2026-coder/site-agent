# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""SQL 契约层：语句与台账策略不经过真实 D1，用记录型假库断言生成结果。"""

from __future__ import annotations

import time

import pytest

from src.app.tools import (
    distribution_tools,
    ledger_tools,
    measurement_tools,
    promotion_tools,
    publishing_tools,
)
from src.app.tools.adapters.cloudflare.sql import (
    LEDGER_TABLES,
    PAGE_QUERIES,
    Sql,
    SqlBuilder,
)
from src.app.tools.registry import RuntimeService

# 实现按域分散在各 *_tools.py，假库要逐个模块替换
DB_MODULES = (distribution_tools, ledger_tools, measurement_tools,
              promotion_tools, publishing_tools)


class RecordingDb:
    """只记录 SQL 的假库；query 返回调用方预设的结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list]] = []
        self.query_result: list[dict] = []

    def query(self, sql, params=None):
        self.calls.append(("query", sql, list(params or [])))
        return self.query_result

    def execute(self, sql, params=None):
        self.calls.append(("execute", sql, list(params or [])))


@pytest.fixture
def db(monkeypatch):
    recorder = RecordingDb()
    for module in DB_MODULES:
        monkeypatch.setattr(module, "database", recorder)
    return recorder


def test_every_ledger_spec_is_consistent():
    for table, spec in LEDGER_TABLES.items():
        assert spec.cols, table
        assert spec.unique, table
        assert set(spec.unique) <= set(spec.cols), table
        assert spec.strategy in ("upsert", "ignore", "lookup"), table


def test_every_page_query_mode_is_renderable():
    assert set(PAGE_QUERIES) == {"all", "recent", "new", "review"}
    for query in PAGE_QUERIES.values():
        assert "{" not in query.sql and "%s" not in query.sql


def test_upsert_strategy_builds_on_conflict_update(db):
    sql = SqlBuilder.write("agent_pages", ["site", "slug", "keyword"], LEDGER_TABLES["agent_pages"])
    assert sql.startswith("INSERT INTO agent_pages (site, slug, keyword) VALUES (?, ?, ?)")
    assert "ON CONFLICT(site, slug) DO UPDATE SET keyword = excluded.keyword" in sql


def test_ignore_strategy_builds_do_nothing(db):
    sql = SqlBuilder.write("agent_events", ["event_key", "kind"], LEDGER_TABLES["agent_events"])
    assert sql.endswith("ON CONFLICT(event_key) DO NOTHING")


def test_write_with_only_unique_cols_falls_back_to_self_update():
    sql = SqlBuilder.write("agent_events", ["event_key"], LEDGER_TABLES["agent_events"])
    assert "DO NOTHING" in sql  # ignore 策略不看列集合

    sql = SqlBuilder.write("agent_reflections", ["event_key"], LEDGER_TABLES["agent_reflections"])
    assert sql.endswith("DO UPDATE SET event_key = excluded.event_key")


def test_lookup_strategy_updates_when_row_exists(db):
    db.query_result = [{"rowid": 1}]
    result = RuntimeService().ledger_upsert(
        "agent_backlinks", {"event_key": "k1", "channel": "devto", "status": "live"})

    assert result["updated"] is True
    assert db.calls[0][1] == "SELECT rowid FROM agent_backlinks WHERE event_key = ?"
    kind, sql, params = db.calls[1]
    assert kind == "execute"
    assert sql == ("UPDATE agent_backlinks SET channel = ?, status = ? "
                   "WHERE event_key = ?")
    assert params == ["devto", "live", "k1"]


def test_lookup_strategy_inserts_when_missing(db):
    db.query_result = []
    result = RuntimeService().ledger_upsert(
        "agent_backlinks", {"event_key": "k2", "channel": "reddit"})

    assert result["inserted"] is True
    # 列顺序按 LedgerSpec.cols 白名单，不按调用方传参顺序
    assert db.calls[1][1] == ("INSERT INTO agent_backlinks (channel, event_key) "
                              "VALUES (?, ?)")
    assert db.calls[1][2] == ["reddit", "k2"]


def test_lookup_without_unique_value_falls_back_to_upsert(db):
    RuntimeService().ledger_upsert("agent_backlinks", {"channel": "devto"})
    assert len(db.calls) == 1
    assert "ON CONFLICT(event_key)" in db.calls[0][1]


def test_unknown_table_and_empty_row_are_rejected(db):
    service = RuntimeService()
    with pytest.raises(ValueError):
        service.ledger_upsert("not_a_table", {"x": 1})
    with pytest.raises(ValueError):
        service.ledger_upsert("agent_pages", {"unlisted": 1})


def test_events_with_kinds_uses_placeholder_list(db):
    RuntimeService().events(kinds=["click", "indexing"], limit=7)
    kind, sql, params = db.calls[0]
    assert "kind IN (?, ?)" in sql
    assert params == ["click", "indexing", 7]


def test_page_inventory_window_only_for_windowing_modes(db):
    service = RuntimeService()

    service.page_inventory(mode="all", limit=5)
    assert db.calls[-1][2] == [5]

    before = int(time.time())
    service.page_inventory(mode="new", limit=5)
    sql, params = db.calls[-1][1], db.calls[-1][2]
    assert "created_at >= ?" in sql and params[1] == 5
    assert before - 30 * 86400 <= params[0] <= int(time.time()) - 30 * 86400 + 2

    service.page_inventory(mode="review", limit=5)
    assert "created_at <= ?" in db.calls[-1][1]


def test_unknown_page_mode_falls_back_to_all(db):
    RuntimeService().page_inventory(mode="nope", limit=3)
    assert "status != 'failed'" in db.calls[-1][1]


def test_fixed_statements_have_no_unformatted_placeholders():
    for name in ("DAILY_STATS", "TOP_PAGES", "SITE_EVENT_COUNTS", "LATEST_PLAN",
                 "BACKLINK_LEDGER", "REFLECTIONS", "PENDING_EVENTS", "CONSUME_EVENT"):
        sql = getattr(Sql, name)
        assert sql.strip(), name
        assert "{" not in sql, name


def test_click_stats_groups_by_site_and_event(db):
    """click_stats 按站点 × 事件类型统计时间窗内数量，并透视成 by_site 字典。"""
    db.query_result = [
        {"site": "a.example", "event": "page_view", "cnt": 30},
        {"site": "a.example", "event": "click", "cnt": 12},
        {"site": "b.example", "event": "page_view", "cnt": 5},
    ]
    out = measurement_tools.MeasurementOps().click_stats(days=7)
    assert out["total_events"] == 47
    assert out["range"]["days"] == 7
    assert out["by_site"] == {
        "a.example": {"page_view": 30, "click": 12},
        "b.example": {"page_view": 5},
    }
    # 原始分组行也要原样保留，供 LLM 直接使用
    assert len(out["by_site_event"]) == 3


def test_click_stats_handles_query_failure(db, monkeypatch):
    """D1 查询异常时降级为空统计，不抛出。"""
    class BoomDb:
        def query(self, *a, **k):
            raise RuntimeError("D1 挂了")
        def execute(self, *a, **k):
            pass
    monkeypatch.setattr(measurement_tools, "database", BoomDb())
    out = measurement_tools.MeasurementOps().click_stats(days=3)
    assert out["total_events"] == 0
    assert out["by_site"] == {}


def test_migration_matches_ledger_contract(tmp_path):
    """migrations/0001 必须与 LEDGER_TABLES 一致：列齐、唯一键对、upsert 能执行。

    这三条任一不满足，线上第一次写入就会报错（D1 与本地 SQLite 行为一致）。
    """
    import sqlite3
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "migrations"
              / "0001_agent_tables.sql").read_text(encoding="utf-8")
    con = sqlite3.connect(tmp_path / "ledger.sqlite")
    con.executescript(script)
    try:
        for table, spec in LEDGER_TABLES.items():
            cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            assert set(spec.cols) <= cols, f"{table} 缺列：{set(spec.cols) - cols}"
            uniques = [tuple(row[2] for row in con.execute(f"PRAGMA index_info({ix[1]})"))
                       for ix in con.execute(f"PRAGMA index_list({table})") if ix[2]]
            assert spec.unique in uniques, f"{table} 缺唯一键 {spec.unique}"
            sql = SqlBuilder.write(table, list(spec.cols), spec)
            con.execute(sql, ["1"] * sql.count("?"))
    finally:
        con.close()
