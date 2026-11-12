# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""sources 适配器的纯逻辑测试：KV 读写、SERP/Trend 纯函数、整理写侧、候选快照。

全部走内存 FakeKV + monkeypatch 外部抓取/LLM，不碰 Cloudflare / Google / DuckDuckGo / OpenAI。
这些模块是「agent 直读共享 KV、去除 console/radar 接口依赖」改造后的核心，必须离线可测。
"""

import json

from src.app.tools.adapters.sources import kv as kvmod
from src.app.tools.adapters.sources import scrape_serp as ss
from src.app.tools.adapters.sources import scrape_trend as st
from src.app.tools.adapters.sources import curation as cur
from src.app.tools.adapters.sources import candidates as cand


# ---------------------------------------------------------------------------
# kv.py：CloudflareKV 的键常量 / term_key / FakeKV 行为
# ---------------------------------------------------------------------------
def test_term_key_normalizes():
    assert kvmod.term_key("  Free  Invoice  CALCULATOR ") == "free invoice calculator"
    assert kvmod.term_key("") == ""


def test_fakekv_roundtrip(fake_kv):
    fake_kv.put_json("k", {"a": 1})
    assert fake_kv.get_json("k") == {"a": 1}
    assert fake_kv.get_json("missing", {"d": 2}) == {"d": 2}
    fake_kv.put_text("t", "hello")
    assert fake_kv.get_text("t") == "hello"
    assert fake_kv.get_text("missing") is None
    fake_kv.put_text("ai:v3:foo", "x")
    assert fake_kv.list_keys("ai:v3:") == ["ai:v3:foo"]
    assert fake_kv.writes == 3  # 两次 put_json(同 key 也算 2) + put_text + put_text


def test_cloudflarekv_transport_url_includes_params():
    # 用假 transport 验证底层 _request 把 prefix/cursor 拼进 URL，且走 transport 分支
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, body))
        return 200, '{"result":{"keys":[]}}'

    client = kvmod.CloudflareKV("acct", "ns", "tok", transport=transport)
    assert client.configured is True
    keys = client.list_keys("ai:v3:")
    assert keys == []
    # list 走 /keys?prefix=...，transport 收到带 query 的 URL
    assert calls and "prefix=ai%3Av3%3A" in calls[-1][1]


# ---------------------------------------------------------------------------
# scrape_serp.py：纯函数（HTML 解析 / 竞争度打分 / verdict）
# ---------------------------------------------------------------------------
DDG_HTML = """
<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.example.com%2Ftool&amp;rut=123">Example <b>Tool</b> Page</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.other.org%2Fpage">Other Page</a>
<a class="result-link" href="https://plain.example.net/x">Plain</a>
</body></html>
"""


def test_parse_ddg_basic():
    items = ss.parse_ddg(DDG_HTML, 10)
    assert len(items) == 3
    assert items[0]["domain"] == "example.com"
    assert "Example Tool Page" in items[0]["title"]
    assert items[1]["domain"] == "other.org"
    assert items[2]["domain"] == "plain.example.net"


def test_decode_uddg():
    assert ss.decode_uddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com") == "https://example.com"
    assert ss.decode_uddg("https://example.com") == "https://example.com"
    assert ss.decode_uddg("//duckduckgo.com/l/?uddg=") == ""


def test_domain_of_strips_www():
    assert ss.domain_of("https://www.Example.com/x") == "example.com"
    assert ss.domain_of("not a url") == ""


def test_serp_verdict_challenge_and_empty():
    v = ss.serp_verdict(202, "<title>DuckDuckGo</title> are you a robot", 10)
    assert v["ok"] is False and v["kind"] == "challenge"
    e = ss.serp_verdict(200, "No results found for xyz", 10)
    assert e["ok"] is True and e["kind"] == "empty"


def test_score_competition_levels():
    items = [
        {"domain": "wikipedia.org", "title": "X"},
        {"domain": "nytimes.com", "title": "Y"},
        {"domain": "bbc.com", "title": "Z"},
        {"domain": "forbes.com", "title": "W"},
        {"domain": "reddit.com", "title": "Forum"},
    ]
    comp = ss.score_competition(items, "example term")
    assert 5 <= comp["score"] <= 95
    assert comp["level"] in ("低", "中", "较高", "高")
    assert comp["counts"]["wiki"] == 1
    assert comp["counts"]["media"] == 3  # nytimes/bbc/forbes；wikipedia 算 auth 不计入 media
    # 论坛在前 10 → 扣分、理由里提到社区
    assert any("社区" in r for r in comp["reasons"])


# ---------------------------------------------------------------------------
# scrape_trend.py：纯函数（XSSI 剥离 / widget 定位 / CSV 解析 / 摘要）
# ---------------------------------------------------------------------------
def test_strip_xssi():
    # 忠实移植 radar/src/interest.js：slice(indexOf("\n")+1) 会留一个前导换行，
    # JSON.parse / json.loads 自动容忍，不影响解析；这里用 strip 比对去端口。
    assert st.strip_xssi(")]}',\n\n{\"a\":1}").strip() == "{\"a\":1}"
    assert st.strip_xssi("plain json") == "plain json"


def test_find_timeseries_widget():
    payload = json.dumps({"widgets": [
        {"id": "OTHER", "token": "", "request": {}},
        {"id": "TIMESERIES", "token": "TOK", "request": {"foo": 1}},
    ]})
    w = st.find_timeseries_widget(")]}',\n\n" + payload)
    assert w and w["token"] == "TOK" and w["request"] == {"foo": 1}
    assert st.find_timeseries_widget("not json") is None


def test_parse_interest_csv():
    csv = (")]}',\n\n"
           "Day,free invoice calculator\n"
           "2026-01-01,50\n2026-01-02,60\n2026-01-03,\n2026-01-04,70\n"
           "2026-01-05,40\n2026-01-06,30\n2026-01-07,20")
    pts = st.parse_interest_csv(csv)
    assert pts and len(pts) == 7  # 至少 5 条才返回
    assert pts[0]["v"] == 50
    assert pts[2]["v"] is None
    assert st.parse_interest_csv("garbage") is None
    assert st.parse_interest_csv(")]}',\n\nDay,x\n2026-01-01,1") is None  # 不足 5 条


def test_summarize_interest():
    pts = [{"d": f"2026-01-{i:02d}", "v": 10 + i} for i in range(1, 15)]
    s = st.summarize_interest(pts)
    assert s["max"] == 24  # v=10+i, i∈[1,14]
    assert s["direction"] in ("up", "down", "flat")
    assert s["delta"] is not None


# ---------------------------------------------------------------------------
# curation.py：整理写侧（与 console/src/store.js 同口径）
# ---------------------------------------------------------------------------
def test_upsert_saved_dedup_and_keep_at():
    key = cur.term_key("Free Invoice Calculator")
    arr = cur.upsert_saved([], {"term": "Free Invoice Calculator", "status": "todo", "related": ["a"]})
    assert len(arr) == 1 and arr[0]["key"] == key and arr[0]["status"] == "todo"
    arr = cur.upsert_saved(arr, {"term": "free invoice calculator", "status": "wip"})
    assert len(arr) == 1  # 同词去重
    assert arr[0]["status"] == "wip" and "at" in arr[0] and "updatedAt" in arr[0]
    # 非法 status 回落到已有 / 默认
    arr = cur.upsert_saved([], {"term": "x", "status": "bogus"})
    assert arr[0]["status"] == "saved"


def test_upsert_rejected_manual_priority():
    arr = cur.upsert_rejected([], {"term": "Foo", "reason": "manual reason", "source": "manual"})
    arr = cur.upsert_rejected(arr, {"term": "Foo", "reason": "ai reason", "source": "ai"})
    assert arr[0]["source"] == "manual"
    assert arr[0]["reason"] == "manual reason"  # AI 不覆盖人工标记


def test_set_hint_override_filters_names():
    m = cur.set_hint_override({}, "Foo", ["工具查询", "不存在", "价格规格", "日程日期"])
    assert m[cur.term_key("Foo")] == ["工具查询", "价格规格", "日程日期"]  # 只留 HINT_NAMES 内、最多 3


# ---------------------------------------------------------------------------
# candidates.py：只读快照（直读 KV，不调 radar /api/candidates）
# ---------------------------------------------------------------------------
def test_snapshot_reads_kv(fake_kv):
    fake_kv.put_json("candidates", [{"term": "a"}, {"term": "b"}])
    fake_kv.put_json("meta", {"updatedMs": 123, "geosOk": 5, "raw": 2907})
    fake_kv.put_json("interests", {"a": {"summary": "x"}})
    snap = cand.snapshot(kv=fake_kv)
    assert len(snap["items"]) == 2
    assert snap["meta"]["geosOk"] == 5
    assert snap["interests"]["a"]["summary"] == "x"
    # light=True 仍回 interests 字段，只是代码路径里不读 KV（保证调用方契约稳定）
    snap2 = cand.snapshot(light=True, kv=fake_kv)
    assert "interests" in snap2

    # 异常兜底：KV 抛错返回空结构不崩溃
    class BoomKV:
        def get_json(self, *a, **k):
            raise RuntimeError("boom")
    failed = cand.snapshot(kv=BoomKV())
    assert failed["items"] == [] and "error" in failed


def test_snapshot_status_filter_keeps_default_doing(fake_kv):
    fake_kv.put_json("candidates", [
        {"term": "a", "status": "doing"},
        {"term": "b", "status": "live"},
        {"term": "c"},  # 无 status 视为 doing
    ])
    snap = cand.snapshot(status="doing", kv=fake_kv)
    terms = {i["term"] for i in snap["items"]}
    assert terms == {"a", "c"}  # 默认 doing；b(live) 被过滤，但 c 无字段不丢
