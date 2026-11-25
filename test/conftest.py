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
from src.app.tools.promotion_tools import PromotionOps
from src.app.tools.registry import RuntimeService, tool_registry

class FakeRuntime(PromotionOps):
    """内存台账替身。继承 PromotionOps 只为拿到 write_promo_slot 的真实现：
    它只用 self.latest_plan / self.ledger_upsert，两者都被下面的内存版覆盖。"""

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
    # 说明：生产里 HotwordsOps.hotwords 已不再委托给这里的同名方法（2026-11-12 去接口化：
    # 改由 adapters/hotwords/wordlib 直读共享 KV）。但 FakeRuntime 作为 RuntimeService 的
    # 测试替身，仍需提供 hotwords 数据桩——get_hotwords 工具经 tool_registry 走到
    # self.ops.hotwords。真正的 KV 读写由 test_kv_hotwords.py + test_hotwords.py 经 fake_kv
    # 夹具覆盖；热词线的 next_candidate / fetch_serp / fetch_trend / write_hotword_result
    # 同样走真实 Ops + fake_kv，这里不另立桩。

    def hotwords(self, status="doing", limit=40):
        self.counts["hotwords"] += 1
        return {"items": self.hotwords_items[:limit],
                "meta": self.hotwords_meta, "interests": {}}

    def click_stats(self, days=7):
        return {
            "range": {"since_ms": 0, "until_ms": 0, "days": days},
            "total_events": 28,
            "by_site_event": [
                {"site": "music.example", "event": "page_view", "cnt": 20},
                {"site": "music.example", "event": "click", "cnt": 8},
            ],
            "by_site": {"music.example": {"page_view": 20, "click": 8}},
        }

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

    def live_page(self, site_name, slug, expect="", sha=""):
        return {"ok": True, "url": f"https://{site_name}.example/{slug}?v=1"}

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
def fake_kv(monkeypatch):
    """内存 KV 替身：生产函数一律自己取 get_kv()，测试只替换取数点。

    热词线的 IO 已全部收进工具（next_candidate / read_examples / fetch_serp /
    fetch_trend / write_hotword_result），句柄由这些适配器自取，所以逐个 patch
    它们的 get_kv 引用；state 不再携带 kv 句柄。建页线（page_build_service）
    同样持有自己的 get_kv 绑定，一并 patch。
    """
    from src.app.services import page_build_service
    from src.app.tools.adapters.cloudflare import kv as kvmod
    from src.app.tools.adapters.hotwords import (
        fetch, wordlib, writeback,
    )

    fake = FakeKV()
    for mod in (kvmod, wordlib, fetch, writeback, page_build_service):
        monkeypatch.setattr(mod, "get_kv", lambda: fake)
    return fake


@pytest.fixture
def fake():
    rt = FakeRuntime()
    tool_registry.bind(rt)
    yield rt
    tool_registry.bind(RuntimeService())


@pytest.fixture
def graph(fake):
    from src.app.graph import build_normal_graph
    return build_normal_graph()


# ---------------------------------------------------------------------------
# 提示词 D1 桩（2026-11-17：本地 versions/*.txt 删除后，测试不再读真实文案）
# ---------------------------------------------------------------------------
# 真源在 radar-rules D1、由 /rules/prompts 页面维护，内容随时可变；测试只验证
# 「拼装逻辑 + 结构契约」（8 小节、结论格式、占位符），不再锁真实文案逐字 SHA。
# 桩文本放在 test/ 下（test_prompts.py 的 FORBIDDEN_IN_CODE 只扫 src/app/**/*.py，
# 不扫测试目录），这样生产代码里依然没有任何提示词字面量。
_PROMPT_STUB_SYSTEM_V1 = (
    "候选池分析 system 桩 v1\n"
    "⓪ 先辨形 · ③ 其余情况\n"
    "1) 词义 2) 信号 7) 结论\n"
    "7) 结论：做 / 观望 / 不做\n"
    "8) 信号标签：工具查询、日程日期\n"
)
_PROMPT_STUB_SYSTEM_V2 = (
    "候选池分析 system 桩 v2\n"
    "非工具意图 · 时效资讯 · 热度走弱 · 专名释义\n"
    "观望」是稀缺档\n"
    "1) 词义 2) 信号 7) 结论\n"
    "7) 结论：做 / 观望 / 不做\n"
    "8) 信号标签：工具查询、日程日期\n"
)
_PROMPT_STUB_USER_V1 = (
    "关键词：{term}\n"
    "{related_line}\n"
    "{serp_line}\n"
    "{hints_line}\n"
    "覆盖地区数：{geo_count} 个\n"
    "热度：{traffic}\n"
)
_PROMPT_STUB_USER_V2 = _PROMPT_STUB_USER_V1 + "{examples_line}\n"

PROMPT_STUB_DATA = {
    "prompts": [
        {"task": "intent", "version": "v1", "content": "意图分类 system 桩 v1"},
        {"task": "intent", "version": "v2", "content": "意图分类 system 桩 v2"},
        {"task": "intent_user", "version": "v1", "content": "请求：{input_text}"},
        {"task": "extraction", "version": "v1", "content": "字段抽取 system 桩"},
        {"task": "extraction_user", "version": "v1", "content": "文本：{input_text}"},
        {"task": "hotwords", "version": "v1", "content": _PROMPT_STUB_SYSTEM_V1},
        {"task": "hotwords", "version": "v2", "content": _PROMPT_STUB_SYSTEM_V2},
        {"task": "hotwords_user", "version": "v1", "content": _PROMPT_STUB_USER_V1},
        {"task": "hotwords_user", "version": "v2", "content": _PROMPT_STUB_USER_V2},
        {"task": "review_metrics", "version": "v1",
         "content": "review_metrics 站点表现分析桩：找出表现较好的站点并排名"},
        {"task": "promo_article", "version": "v1",
         "content": "promo_article 外链起稿桩：只输出 JSON {title,tags,target_url,draft}"},
        {"task": "promo_article", "version": "v2",
         "content": "promo_article 外链起稿桩 v2：英文正文 + 只输出 JSON"},
        {"task": "word_plan_page", "version": "v1",
         "content": "word_plan_page 排产桩：输出本轮排产方案（选词/落站/排除原因）"},
        {"task": "word_plan_page", "version": "v4",
         "content": "word_plan_page 排产桩 v4：只输出 JSON {items,excluded}"},
        {"task": "page_content", "version": "v1",
         "content": "page_content 正文写作桩：分析稿→访客可读 JSON {title,description,h1,intro,sections,faq}"},
    ],
    "defaults": [
        {"task": "intent", "version": "v1"},
        {"task": "intent_user", "version": "v1"},
        {"task": "extraction", "version": "v1"},
        {"task": "extraction_user", "version": "v1"},
        {"task": "hotwords", "version": "v2"},
        {"task": "hotwords_user", "version": "v2"},
        {"task": "review_metrics", "version": "v1"},
        {"task": "promo_article", "version": "v2"},
        {"task": "word_plan_page", "version": "v4"},
        {"task": "page_content", "version": "v1"},
    ],
}


@pytest.fixture(autouse=True)
def no_planned_pages(monkeypatch):
    """建页取词默认「没有排产计划」：不替测试去打真实 D1，也让旧用例保持按池顺序取。

    需要计划行本的用例自己 monkeypatch page_build_service._planned_rows。
    """
    from src.app.services import page_build_service

    monkeypatch.setattr(page_build_service, "_planned_rows", lambda limit=5: [])


@pytest.fixture(autouse=True)
def fake_prompt_store(monkeypatch):
    """每个测试都把全局提示词单例指向 D1 桩：无 CF 凭据也能测拼装逻辑。

    只替换 fetcher + 打开 configured 开关，不动 registry 的对象引用（它 import 的
    是同一个单例）。测试内部若自行 monkeypatch configured / prompt_store，优先级更高。
    """
    from src.app.prompts import hotword_messages
    from src.app.prompts.d1_source import D1PromptStore, prompt_store

    def fetch(_account, _token, _db):
        # 深拷贝，避免某测试改动桩数据污染其它测试
        import copy
        return copy.deepcopy(PROMPT_STUB_DATA)

    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(prompt_store, "_fetcher", fetch)
    prompt_store._cache = None
    hotword_messages._prompt_pair.cache_clear()
    yield
    prompt_store._cache = None
    hotword_messages._prompt_pair.cache_clear()
