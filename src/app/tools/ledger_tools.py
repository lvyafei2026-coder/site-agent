# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""ledger 域：Agent 台账 / KV / 事件 / 通知（定义 + 实现同文件）。"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field

from src.app.tools.adapters.cloudflare import database, keyvalue
from src.app.tools.adapters.cloudflare.sql import LEDGER_TABLES, Sql, SqlBuilder
from src.app.tools.adapters.publishing import email
from src.app.tools.base import RuntimeTool, stable_key


class LedgerOps:
    """ledger 域的实现：白名单台账表 + agent: 命名空间 KV。"""

    def reflections(self, limit=8):
        return database.query(Sql.REFLECTIONS, [limit])

    def events(self, kinds=None, limit=50):
        if kinds:
            return database.query(SqlBuilder.pending_events(kinds), [*kinds, limit])
        return database.query(Sql.PENDING_EVENTS, [limit])

    def flag(self, key, default=None):
        return keyvalue.get(key, default)

    def ledger_upsert(self, table, row):
        spec = LEDGER_TABLES.get(table)
        if spec is None:
            raise ValueError(f"未登记的台账表：{table}")
        cols = [c for c in spec.cols if c in row]
        if not cols:
            raise ValueError(f"{table} 没有可写字段")
        key = [row.get(c) for c in spec.unique]

        if spec.strategy == "lookup" and all(c in row for c in spec.unique):
            unique_values = [row[c] for c in spec.unique]
            found = database.query(
                SqlBuilder.select_by_unique(table, spec.unique), unique_values)
            if not found:
                database.execute(SqlBuilder.insert(table, cols),
                                 [row[c] for c in cols])
                return {"table": table, "inserted": True, "key": key}
            updates = [c for c in cols if c not in spec.unique]
            if updates:
                database.execute(
                    SqlBuilder.update_by_unique(table, updates, spec.unique),
                    [row[c] for c in updates] + unique_values,
                )
            return {"table": table, "updated": True, "key": key}

        database.execute(SqlBuilder.write(table, cols, spec), [row[c] for c in cols])
        return {"table": table, "written": len(cols), "key": key}

    def flag_set(self, key, value, ttl_sec=None):
        keyvalue.put(key, value, ttl_sec=ttl_sec)
        return {"key": key, "set": True}

    def event_emit(self, kind, payload, key):
        return self.ledger_upsert("agent_events", {
            "event_key": key, "kind": kind,
            "payload": json.dumps(payload, ensure_ascii=False),
            "status": "pending", "created_at": int(time.time()),
        })

    def event_consume(self, event_key, thread_id):
        database.execute(Sql.CONSUME_EVENT,
                         [int(time.time()), thread_id, event_key])
        return {"event_key": event_key, "consumed": True}

    def reflection_add(self, slot, reason, detail, action_key=""):
        key = stable_key("reflection", slot, action_key, reason, detail,
                         int(time.time() // 300))
        return self.ledger_upsert("agent_reflections", {
            "event_key": key, "slot": slot, "action_key": action_key,
            "reason": reason[:200], "detail": detail[:600],
            "created_at": int(time.time()),
        })

    def send_notice(self, subject, html):
        return {"sent": bool(email.send(subject, html))}


class GetFlagTool(RuntimeTool):
    """读取 agent: 命名空间下的状态标记（熔断、上次处理时间、待审清单）。"""

    tool_name = "get_flag"
    method = "flag"

    class Input(BaseModel):
        key: str = Field(..., description="不含 agent: 前缀的键")


class GetReflectionsTool(RuntimeTool):
    """读取最近的失败复盘 / 效果反思记录，用于指导下一次选题与决策。"""

    tool_name = "get_reflections"
    method = "reflections"

    class Input(BaseModel):
        limit: int = Field(8, description="最多返回几条，默认 8")


class GetPendingEventsTool(RuntimeTool):
    """读取 agent_events 里尚未消费的事件（新热词、新页上线、排期完成等）。"""

    tool_name = "get_pending_events"
    method = "events"

    class Input(BaseModel):
        limit: int = Field(50, description="最多返回几条")


class WriteLedgerTool(RuntimeTool):
    """把一条记录 UPSERT 进 Agent 台账（白名单表与字段，禁止裸 SQL）。"""

    tool_name = "write_ledger"
    method = "ledger_upsert"
    kind = "ledger"

    class Input(BaseModel):
        table: str = Field(..., description="agent_pages|agent_reports|agent_backlinks|agent_events|...")
        row: dict[str, Any] = Field(..., description="字段字典")


class SetFlagTool(RuntimeTool):
    """写 agent: 命名空间的状态标记（上次处理时间、待审清单等）。"""

    tool_name = "set_flag"
    method = "flag_set"
    kind = "ledger"

    class Input(BaseModel):
        key: str = Field(..., description="键")
        value: Any = Field(..., description="任意 JSON 值")
        ttl_sec: int | None = Field(None, description="过期秒数")


class EmitEventTool(RuntimeTool):
    """往 agent_events 写一条事件；event_key 是幂等键，重复写被忽略。"""

    tool_name = "emit_event"
    method = "event_emit"
    kind = "ledger"

    class Input(BaseModel):
        kind: str = Field(..., description="hotwords-ready|pages-deployed|promo-scheduled|backlinks-found|metrics-ready|reflection")
        payload: dict[str, Any] = Field(..., description="事件内容")
        key: str = Field(..., description="幂等键")


class RecordReflectionTool(RuntimeTool):
    """记录一次失败原因或效果反思，供下一轮决策读取。"""

    tool_name = "record_reflection"
    method = "reflection_add"
    kind = "ledger"

    class Input(BaseModel):
        slot: str = Field(..., description="来源标识：哪个能力写入的")
        reason: str = Field(..., description="简短原因")
        detail: str = Field(..., description="细节")
        action_key: str = Field('', description="关联动作 key")


class ConsumeEventTool(RuntimeTool):
    """把 event_key 对应的事件标记为 consumed。"""

    tool_name = "consume_event"
    method = "event_consume"
    kind = "ledger"

    class Input(BaseModel):
        event_key: str = Field(..., description="事件幂等键")
        thread_id: str = Field('', description="消费方 thread_id")


class SendNoticeTool(RuntimeTool):
    """给运营者发一封站内通知邮件（日报、告警、已批准草稿）。"""

    tool_name = "send_notice"
    method = "send_notice"
    kind = "ledger"

    class Input(BaseModel):
        subject: str = Field(..., description="标题")
        html: str = Field(..., description="HTML 正文")
