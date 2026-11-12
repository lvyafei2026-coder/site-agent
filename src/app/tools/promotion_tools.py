# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""promotion 域：推广排期读写（定义 + 实现同文件）。"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field

from src.app.tools.adapters.cloudflare import database
from src.app.tools.adapters.cloudflare.sql import Sql
from src.app.tools.base import RuntimeTool, ToolOutput


class PromotionOps:
    """promotion 域的实现：D1 里的 agent_reports(kind='plan')。"""

    def latest_plan(self):
        rows = database.query(Sql.LATEST_PLAN)
        if not rows:
            return []
        try:
            return json.loads(rows[0].get("actions") or "[]")
        except (TypeError, ValueError):
            return []


class GetLatestPlanTool(RuntimeTool):
    """读取最近一份推广周排期，用于避免重复排期。"""

    tool_name = "get_latest_plan"
    method = "latest_plan"

    class Input(BaseModel):
        pass


class WritePromoSlotTool(RuntimeTool):
    """把一条推广排期写进 agent_reports(kind='plan')，带同日同渠道查重。"""

    tool_name = "write_promo_slot"
    kind = "ledger"

    class Input(BaseModel):
        item: dict[str, Any] = Field(..., description="排期条目")

    def run(self, **kwargs: Any) -> dict[str, Any]:
        # 26 个工具里唯一带业务分支的（先读后判重再写），所以覆盖 run()。
        try:
            item = self.Input(**kwargs).item
            current = self.ops.latest_plan() or []
            if any(p.get("date") == item.get("date")
                   and p.get("channel") == item.get("channel") for p in current):
                return ToolOutput(tool=self.name,
                                  result={"status": "skipped", "detail": "排期已存在"}).model_dump()
            current.append(item)
            self.ops.ledger_upsert("agent_reports", {
                "report_date": time.strftime("%Y-%m-%d"), "kind": "plan",
                "summary": f"下周排期（{len(current)} 条）", "anomalies": "[]",
                "actions": json.dumps(current, ensure_ascii=False),
                "created_at": int(time.time()),
            })
            return ToolOutput(tool=self.name,
                              result={"status": "scheduled",
                                      "detail": item.get("title", "")}).model_dump()
        except Exception as exc:  # noqa: BLE001
            return ToolOutput(tool=self.name, ok=False,
                              error=str(exc)[:300]).model_dump()
