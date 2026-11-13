# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""测试基建：内存版 RuntimeService，测试不碰 Cloudflare / GitHub / OpenAI。"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

import pytest

from src.app.tools.adapters.cloudflare.sql import LEDGER_TABLES
from src.app.tools.registry import RuntimeService, tool_registry

class FakeRuntime:
    def __init__(self, hotwords_meta: dict | None = None):
        self.kv: dict[str, Any] = {}
        self.tables: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.counts: dict[str, int] = defaultdict(int)
        self.hotwords_meta = hotwords_meta or {
            "updatedMs": 1_700_000_000_000, "geosOk": 16, "raw": 2907,
        }
        self.hotwords_items = [
            {"term": "free invoice calculator", "status": "doing", "traffic": 900},
            {"term": "Taylor Swift net worth", "status": "doing", "traffic": 8000},
        ]

    # ---- 读 ----
    # 说明：生产里 SelectionOps.hotwords / analyze_hotwords 已不再委托给这里的同名方法
    #（2026-11-12 去接口化：改由 agent 直读共享 KV，见 sources/kv.py）。但 FakeRuntime 作为
    # RuntimeService 的测试替身，仍需提供 hotwords 数据桩——get_hotwords 工具经 tool_registry
    # 走到 self.ops.hotwords。真正的 KV 读写由 test/test_kv_sources.py + test_hotwords.py
    # 经 fake_kv 夹具覆盖。analyze_hotwords 现由 run_batch 直接测，这里不另立桩。

    def hotwords(self, status="doing", limit=40):
        self.counts["hotwords"] += 1
        return {"items": self.hotwords_items[:limit],
                "meta": self.hotwords_meta, "interests": {}}

    def click_stats(self, days=7):
        return {"daily": [{"site": "music.example", "day": 1, "pv": 20, "uv": 8}],
                "top_pages": [{"site": "music.example", "path": "/", "pv": 20, "uv": 8}]}

    def page_inventory(self, mode="all", limit=200):
        rows = list(self.tables["agent_pages"])
        if mode == "review":
            rows = [r for r in rows if r.get("gsc_clicks") is not None]
        return rows[:limit]

    def latest_plan(self):
        plans = [r for r in self.tables["agent_reports"] if r.get("kind") == "plan"]
        if not plans:
            return []
        try:
            return json.loads(plans[-1].get("actions") or "[]")
        except ValueError:
            return []

    def backlink_ledger(self, limit=200):
        return [r for r in self.tables["agent_backlinks"]
                if r.get("status") != "dead"][:limit]

    def search_analytics(self, days=7, dimensions=("page",)):
        return []

    def reddit_search(self, query, limit=8):
        return [{"title": f"thread about {query}", "url": "https://reddit.com/x",
                 "subreddit": "test", "score": 3, "num_comments": 2}]

    def devto_articles(self, tags):
        return [{"channel": "devto", "title": "AI tools", "url": "https://dev.to/x",
                 "tags": tags, "reactions": 12}]

    def build_status(self, repo, sha):
        return [{"name": "Workers Builds: test", "status": "completed",
                 "conclusion": "success"}]

    def live_page(self, site_name, slug, expect=""):
        return {"ok": True, "url": f"https://{site_name}.example/{slug}.html?v=1"}

    def flag(self, key, default=None):
        value = self.kv.get(key, default)
        if isinstance(value, dict) and value.get("__ttl__"):
            return default
        return value

    def reflections(self, limit=8):
        return list(reversed(self.tables["agent_reflections"]))[:limit]

    def events(self, kinds=None, limit=50):
        rows = [r for r in self.tables["agent_events"] if r.get("status") == "pending"]
        if kinds:
            rows = [r for r in rows if r.get("kind") in kinds]
        return rows[:limit]

    # ---- 台账 ----
    def _find(self, table: str, unique: tuple[str, ...], row: dict) -> dict | None:
        for existing in self.tables[table]:
            if all(existing.get(c) == row.get(c) for c in unique if c in row):
                return existing
        return None

    def ledger_upsert(self, table, row):
        self.counts[f"write:{table}"] += 1
        spec = LEDGER_TABLES[table]
        cols = [c for c in spec.cols if c in row]
        unique = spec.unique
        found = self._find(table, unique, row)
        if found:
            if spec.strategy != "ignore":
                for c in cols:
                    if c not in unique:
                        found[c] = row[c]
            return {"table": table, "updated": True}
        record = {c: row[c] for c in cols}
        record.setdefault("created_at", int(time.time()))
        self.tables[table].append(record)
        return {"table": table, "inserted": True}

    def flag_set(self, key, value, ttl_sec=None):
        self.kv[key] = value
        return {"key": key, "set": True}

    def event_emit(self, kind, payload, key):
        # 与生产门面一致：走 ledger_upsert，靠 event_key 唯一键 + ignore 策略幂等，
        # 而不是无条件 append（事件幂等由 D1 的 UNIQUE(event_key) 保证）。
        return self.ledger_upsert("agent_events", {
            "event_key": key, "kind": kind, "payload": json.dumps(payload),
            "status": "pending", "created_at": int(time.time()),
        })

    def event_consume(self, event_key, thread_id):
        for row in self.tables["agent_events"]:
            if row.get("event_key") == event_key:
                row["status"] = "consumed"
                row["thread_id"] = thread_id
        return {"event_key": event_key, "consumed": True}

    def reflection_add(self, slot, reason, detail, action_key=""):
        self.tables["agent_reflections"].append({
            "slot": slot, "action_key": action_key, "reason": reason,
            "detail": detail, "created_at": int(time.time()),
        })
        return {"slot": slot}

    # ---- 写 ----
    def render_files(self, site_name, page):
        self.counts["render_files"] += 1
        return {"ok": True, "work": "/tmp/fake-repo", "slug": page["slug"],
                "changed": True, "repo": f"org/{site_name}", "branch": "main"}

    def git_push(self, site_name, page, work):
        self.counts["git_push"] += 1
        return {"ok": True, "skipped": False, "sha": "deadbeef",
                "repo": f"org/{site_name}", "branch": "main"}

    def wait_build(self, repo, sha, work="", branch="main"):
        self.counts["wait_build"] += 1
        return {"ok": True, "sha": sha, "detail": "构建成功"}

    def queue_indexing(self, url):
        self.counts["queue_indexing"] += 1
        return {"url": url}

    def publish_devto(self, payload):
        self.counts["publish_devto"] += 1
        return {"ok": True, "status": "published",
                "source_url": "https://dev.to/fake", "detail": "published"}

    def send_notice(self, subject, html):
        self.counts["send_notice"] += 1
        return {"sent": True}


class FakeKV:
    """内存版 KV，接口与 kv.CloudflareKV 对齐：测试不碰 Cloudflare。

    支持 get_text/get_json/put_text/put_json/list_keys/configured；TTL 在内存实现里
    被忽略（纯逻辑测试不关心过期）。writes 记录写入次数，便于断言「是否真的回写了 KV」。
    fail_keys 里的键会模拟 Cloudflare 401/5xx：put_* 返回 False 且不落库，用来验证
    「写失败 → 该词记 error、不算 analyzed」。
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.writes = 0
        self.fail_keys: set[str] = set()

    @property
    def configured(self) -> bool:
        return True

    def get_text(self, key: str, default=None):
        return self.store.get(key, default)

    def get_json(self, key: str, default=None):
        text = self.store.get(key)
        if text is None:
            return default
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return default

    def put_text(self, key: str, text: str, ttl=None) -> bool:
        if key in self.fail_keys:
            return False
        self.store[key] = str(text)
        self.writes += 1
        return True

    def put_json(self, key: str, obj, ttl=None) -> bool:
        if key in self.fail_keys:
            return False
        self.store[key] = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        self.writes += 1
        return True

    def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self.store if k.startswith(prefix)]


@pytest.fixture
def fake_kv():
    return FakeKV()


@pytest.fixture
def fake():
    rt = FakeRuntime()
    tool_registry.bind(rt)
    yield rt
    tool_registry.bind(RuntimeService())


@pytest.fixture
def graph(fake):
    from src.app.graph.builder import build_graph
    return build_graph()
