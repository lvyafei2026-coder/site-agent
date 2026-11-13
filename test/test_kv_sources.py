# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""sources 适配器的纯逻辑测试：KV 读写、SERP/Trend 纯函数、整理写侧、候选快照。

全部走内存 FakeKV + monkeypatch 外部抓取/LLM，不碰 Cloudflare / Google / DuckDuckGo / OpenAI。
这些模块是「agent 直读共享 KV、去除 console/radar 接口依赖」改造后的核心，必须离线可测。
"""

import json

import pytest

from src.app.tools.adapters.sources import kv as kvmod
from src.app.tools.adapters.sources import scrape_serp as ss
from src.app.tools.adapters.sources import scrape_trend as st
from src.app.tools.adapters.sources import curation as cur
from src.app.tools.adapters.sources import candidates as cand
from src.app.tools.adapters.sources.errors import SourceUnavailable


@pytest.fixture(autouse=True)
def _reset_serp_egress_state():
    """SERP「直连出口不通」是进程级状态，每个用例前后清零，避免跨用例污染。"""
    ss._STATE["direct_down_until"] = 0.0
    yield
    ss._STATE["direct_down_until"] = 0.0


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
        return 200, '{"result":[],"result_info":{"count":0}}'

    client = kvmod.CloudflareKV("acct", "ns", "tok", transport=transport)
    assert client.configured is True
    keys = client.list_keys("ai:v3:")
    assert keys == []
    # list 走 /keys?prefix=...，transport 收到带 query 的 URL
    assert calls and "prefix=ai%3Av3%3A" in calls[-1][1]


def test_list_keys_real_cloudflare_shape():
    """★真实 Cloudflare 格式：result 是数组、游标在 result_info.cursor。

    曾经按 {"result":{"keys":[...]}} 解析，线上报 "'list' object has no attribute 'get'"。
    """
    def transport(method, url, headers, body):
        return 200, json.dumps({
            "result": [{"name": "ai:v3:foo"}, {"name": "ai:v3:bar"}],
            "success": True,
            "result_info": {"count": 2, "cursor": ""},
        })

    client = kvmod.CloudflareKV("acct", "ns", "tok", transport=transport)
    assert client.list_keys("ai:v3:") == ["ai:v3:foo", "ai:v3:bar"]


def test_list_keys_follows_cursor_in_result_info():
    """游标在 result_info 里，必须跟着翻页，否则只拿到第一页 1000 条。"""
    pages = [
        {"result": [{"name": "a1"}], "result_info": {"cursor": "c1"}},
        {"result": [{"name": "a2"}], "result_info": {"cursor": "c2"}},
        {"result": [{"name": "a3"}], "result_info": {"count": 1}},
    ]
    seen = []

    def transport(method, url, headers, body):
        seen.append(url)
        return 200, json.dumps(pages[len(seen) - 1])

    client = kvmod.CloudflareKV("acct", "ns", "tok", transport=transport)
    assert client.list_keys("a") == ["a1", "a2", "a3"]
    assert len(seen) == 3
    assert "cursor=c1" in seen[1] and "cursor=c2" in seen[2]


def test_list_keys_tolerates_legacy_dict_shape():
    """兜底：老式 {"result":{"keys":[...],"cursor":"x"}} 也能解析。"""
    def transport(method, url, headers, body):
        return 200, '{"result":{"keys":[{"name":"k1"}],"cursor":"x"}}'

    client = kvmod.CloudflareKV("acct", "ns", "tok", transport=transport)
    assert client.list_keys("k")[:1] == ["k1"]


def test_list_keys_handles_error_and_garbage():
    """非 200、非 JSON、result 类型异常都不能抛，返回已拿到的部分。"""
    for body in ('', '不是 JSON', '{"result":null}', '[1,2,3]'):
        client = kvmod.CloudflareKV("acct", "ns", "tok", transport=lambda *a, _b=body: (200, _b))
        assert client.list_keys("x") == []

    client = kvmod.CloudflareKV("acct", "ns", "tok", transport=lambda *a: (500, 'oops'))
    assert client.list_keys("x") == []


def test_production_path_builds_correct_request(monkeypatch):
    """★线上走 requests 分支（transport=None），必须编码键名、snake_case TTL、不发 __list__。"""
    seen = []

    class FakeResp:
        status_code = 200
        text = '{"success":true,"result":[],"result_info":{}}'

    def fake_request(method, url, params=None, headers=None, data=None, timeout=None):
        seen.append(("request", method, url, params, data))
        return FakeResp()

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append(("get", url, params))
        return FakeResp()

    monkeypatch.setattr(kvmod.requests, "request", fake_request)
    monkeypatch.setattr(kvmod.requests, "get", fake_get)

    client = kvmod.CloudflareKV("acct", "ns", "tok")  # 无 transport → 生产分支
    client.put_text("serp:v1:a b", "{}", ttl=604800)
    kind, method, url, params, data = seen[-1]
    assert kind == "request" and method == "PUT"
    assert url.endswith("/values/serp%3Av1%3Aa%20b")
    assert params == {"expiration_ttl": 604800}
    assert data == "{}"

    client.list_keys("ai:v3:")
    kind, url, params = seen[-1]
    assert kind == "get" and url.endswith("/keys")
    assert params["prefix"] == "ai:v3:" and "__list__" not in params


def test_kv_key_is_percent_encoded():
    """★键名含冒号/空格/# 时必须编码，否则 URL 被截断（词是自然语言短语）。"""
    urls = []

    def transport(method, url, headers, body):
        urls.append(url)
        return 200, '{"success":true,"result":{}}'

    client = kvmod.CloudflareKV("acct", "ns", "tok", transport=transport)
    key = "ai:v3:" + kvmod.term_key("Free Invoice #Calculator?")
    client.put_text(key, "x")
    assert "/values/ai%3Av3%3Afree%20invoice%20%23calculator%3F" in urls[-1]
    assert " " not in urls[-1] and "#" not in urls[-1] and "?" not in urls[-1]


def test_put_text_uses_snake_case_expiration_ttl():
    """★REST 写 KV 的 TTL 参数是 expiration_ttl（snake_case），不是 expirationTtl。"""
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, body))
        return 200, '{"success":true,"result":{}}'

    client = kvmod.CloudflareKV("acct", "ns", "tok", transport=transport)
    assert client.put_text("serp:v1:foo", "{}", ttl=604800) is True
    assert "expiration_ttl=604800" in calls[-1][1]
    assert "expirationTtl" not in calls[-1][1]

    # 不传 ttl 就不带任何过期参数（无 TTL = 永不过期）
    assert client.put_text("ai:v3:foo", "x") is True
    assert "expiration" not in calls[-1][1]

    # 小于 60 秒会被 Cloudflare 拒绝，这里抬到下限
    assert client.put_text("k", "v", ttl=5) is True
    assert "expiration_ttl=60" in calls[-1][1]


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


def test_parse_interest_csv_handles_lt1():
    # Google Trends 对极低兴趣度返回 "<1"，之前直接 float() 会抛 ValueError；
    # 现在应归一为 0（整数尺度下取整），不再崩溃。
    csv = (")]}',\n\n"
           "Day,term\n"
           "2026-01-01,<1\n2026-01-02,<1\n2026-01-03,<1\n"
           "2026-01-04,<1\n2026-01-05,<1\n2026-01-06,2\n2026-01-07,3")
    pts = st.parse_interest_csv(csv)
    assert pts is not None
    assert pts[0]["v"] == 0
    assert all(p["v"] == 0 for p in pts[:5])


def test_to_int_tolerates_edge_values():
    assert st._to_int("<1") == 0
    assert st._to_int("") is None
    assert st._to_int("   ") is None
    assert st._to_int("57") == 57
    assert st._to_int(">100") == 100  # 抽首位数字兜底
    assert st._to_int("n/a") is None


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


def test_snapshot_survives_shape_drift(fake_kv):
    """★candidates / meta / interests 形状漂移时按空处理，不让工具调用整体失败。"""
    fake_kv.put_json("candidates", {"a": {"term": "a"}})   # 期望数组
    fake_kv.put_json("meta", ["x"])                         # 期望对象
    fake_kv.put_json("interests", "垃圾")                   # 期望对象
    snap = cand.snapshot(kv=fake_kv)
    assert snap["items"] == [] and snap["meta"] == {} and snap["interests"] == {}


def test_snapshot_status_filter_keeps_default_doing(fake_kv):
    fake_kv.put_json("candidates", [
        {"term": "a", "status": "doing"},
        {"term": "b", "status": "live"},
        {"term": "c"},  # 无 status 视为 doing
    ])
    snap = cand.snapshot(status="doing", kv=fake_kv)
    terms = {i["term"] for i in snap["items"]}
    assert terms == {"a", "c"}  # 默认 doing；b(live) 被过滤，但 c 无字段不丢


# ---------------------------------------------------------------------------
# 源级不可用：连不上 / 被挑战页占满 → 抛 SourceUnavailable，上层才能中止整批
# ---------------------------------------------------------------------------
class _DownSession:
    """模拟 Render 出口连不上 DDG / Trends：一律 ConnectTimeout。"""

    def get(self, *a, **k):
        import requests
        raise requests.exceptions.ConnectTimeout("Connection to duckduckgo.com timed out")


class _ChallengeSession:
    """能连上，但每次都回 202 挑战页。"""

    class _R:
        status_code = 202
        text = "<html><head><title>DuckDuckGo</title></head><body></body></html>"
        ok = True

    def get(self, *a, **k):
        return self._R()


def test_fetch_serp_raises_source_unavailable_on_connect_failure(monkeypatch):
    """连不上 DDG（Render 出口实测）→ 源级不可用，上层中止整批。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    with pytest.raises(SourceUnavailable) as ei:
        ss.fetch_serp("cba housing market downturn outlook", session=_DownSession())
    assert ei.value.source == "duckduckgo"
    assert "连不上" in str(ei.value)
    assert len(ei.value.attempts) == 4          # 4 发都记了账
    assert all(a["status"] == 0 for a in ei.value.attempts)


def test_fetch_serp_raises_source_unavailable_on_challenge_pages(monkeypatch):
    """被判定异常时仍可能回 202 挑战页 → 仍是源级不可用，不能当「无结果」。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    with pytest.raises(SourceUnavailable) as ei:
        ss.fetch_serp("some term", session=_ChallengeSession())
    assert ei.value.source == "duckduckgo"
    assert "未返回可用结果" in str(ei.value)
    assert all(a["status"] == 202 for a in ei.value.attempts)


class _MinimalHeaderSession:
    """只认最小头的假 DDG：多带头就回 202，用来把 11-13 的定位钉成回归测试。"""

    class _R:
        def __init__(self, status: int, text: str):
            self.status_code = status
            self.text = text

    def get(self, url, headers=None, **kwargs):
        if set(headers or {}) - {"User-Agent"}:
            return self._R(202, "<title>DuckDuckGo</title> are you a robot")
        return self._R(200, DDG_HTML)


def test_fetch_serp_uses_only_user_agent(monkeypatch):
    """2026-11-13：202 挑战页是显式 Accept / Accept-Language / Referer 触发的。
    只发 User-Agent 才能拿到真结果；防止有人把「更像浏览器」的头加回来。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    out = ss.fetch_serp("example term", session=_MinimalHeaderSession())
    assert out["engine"] == "duckduckgo"
    assert [x["domain"] for x in out["items"]] == ["example.com", "other.org", "plain.example.net"]
    assert set(ss.BASE_HEADERS) == {"User-Agent"}


class _HtmlSession:
    """按 host 决定成败的假 DDG：用来验证多域名退避，不碰网络。"""

    class _R:
        def __init__(self, status: int, text: str):
            self.status_code = status
            self.text = text

    def __init__(self, down_hosts=()):
        self.down_hosts = set(down_hosts)
        self.seen: list[str] = []
        self.kwargs: list[dict] = []

    def get(self, url, **kwargs):
        import urllib.parse as up
        import requests
        host = up.urlparse(url).hostname
        self.seen.append(host)
        self.kwargs.append(kwargs)
        if host in self.down_hosts:
            raise requests.exceptions.ConnectTimeout("connect timed out")
        return self._R(200, DDG_HTML)


def test_fetch_serp_tries_four_distinct_hosts(monkeypatch):
    """★出口只拦部分子域时，第二发（主域、另一组 IP）就能拿到真结果。
    4 发必须覆盖 4 个不同 host，别退化回两个子域来回撞。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    fake = _HtmlSession(down_hosts=["html.duckduckgo.com"])
    out = ss.fetch_serp("example term", session=fake)
    assert fake.seen == ["html.duckduckgo.com", "duckduckgo.com"]
    assert [x["domain"] for x in out["items"]] == ["example.com", "other.org", "plain.example.net"]
    assert out["engine"] == "duckduckgo"
    assert len({s["host"] for s in ss.PLAN}) == 4


def test_fetch_serp_connect_failure_reports_hosts_and_error_kind(monkeypatch):
    """★Render 出口 4 发全挂时，报错要直接说清「哪些 host、什么异常」，
    别再甩一长串被 UI 截断的 HTTPSConnectionPool 文本。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    with pytest.raises(SourceUnavailable) as ei:
        ss.fetch_serp("petroleum", session=_DownSession())
    msg = str(ei.value)
    assert "连不上" in msg and "ConnectTimeout" in msg
    for host in ("html.duckduckgo.com", "duckduckgo.com",
                 "lite.duckduckgo.com", "www.duckduckgo.com"):
        assert host in msg
    assert len(ei.value.attempts) == 4
    assert {a["host"] for a in ei.value.attempts} == {s["host"] for s in ss.PLAN}
    assert all(a["error"] == "ConnectTimeout" for a in ei.value.attempts)
    assert all(a["status"] == 0 for a in ei.value.attempts)


def test_fetch_serp_honors_dedicated_proxy_env(monkeypatch):
    """SERP_HTTPS_PROXY 只给 SERP 换出口；不配时不能往 kwargs 里塞 proxies，
    否则会连带改掉同进程里 KV REST / LLM 的传输路径。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_HTTPS_PROXY", "")
    plain = _HtmlSession()
    ss.fetch_serp("x", session=plain)
    assert "proxies" not in plain.kwargs[0]

    monkeypatch.setattr(ss, "SERP_HTTPS_PROXY", "http://127.0.0.1:8899")
    proxied = _HtmlSession()
    ss.fetch_serp("x", session=proxied)
    assert proxied.kwargs[0]["proxies"] == {
        "http": "http://127.0.0.1:8899", "https": "http://127.0.0.1:8899"}


class _ReaderSession:
    """直连一律连不上；Reader 出口可按开关给真 HTML / 也连不上。全程离线。"""

    class _R:
        def __init__(self, status: int, text: str):
            self.status_code = status
            self.text = text

    def __init__(self, reader_ok=True, reader_down=False):
        self.reader_ok = reader_ok
        self.reader_down = reader_down
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        import requests
        self.urls.append(url)
        if "r.jina.ai" in url:
            if self.reader_down:
                raise requests.exceptions.ConnectTimeout("reader down")
            return self._R(200, DDG_HTML if self.reader_ok else "<title>DuckDuckGo</title>")
        raise requests.exceptions.ConnectTimeout("direct down")


def test_fetch_serp_falls_back_to_reader_when_direct_all_down(monkeypatch):
    """★Render 出口 4 发全 ConnectTimeout 时，自动换 Reader 出口拿到真结果。
    兜底不能污染直连的 4 发 attempts（上层按 attempts 记账）。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_FALLBACK_READER", True)
    fake = _ReaderSession()
    out = ss.fetch_serp("petroleum", session=fake)
    assert out["via"] == "reader"
    assert out["engine"] == "duckduckgo"     # 写进 KV 的口径不变
    assert [x["domain"] for x in out["items"]] == ["example.com", "other.org", "plain.example.net"]
    assert len(out["attempts"]) == 4
    assert sum("r.jina.ai" in u for u in fake.urls) == 1
    assert fake.urls[-1].startswith(ss.READER_BASE)


def test_fetch_serp_reader_can_be_disabled(monkeypatch):
    """SERP_FALLBACK_READER=0 → 回到纯直连，一个字节都不发给第三方服务。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_FALLBACK_READER", False)
    fake = _ReaderSession()
    with pytest.raises(SourceUnavailable):
        ss.fetch_serp("petroleum", session=fake)
    assert not any("r.jina.ai" in u for u in fake.urls)


def test_fetch_serp_reports_reader_failure_when_both_paths_down(monkeypatch):
    """两条出口都挂 → 诊断要点名 Reader，避免又回到「是不是请求头不对」的老猜测。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_FALLBACK_READER", True)
    fake = _ReaderSession(reader_down=True)
    with pytest.raises(SourceUnavailable) as ei:
        ss.fetch_serp("petroleum", session=fake)
    assert "Reader 中转也连不上" in str(ei.value)
    assert "ConnectTimeout" in str(ei.value)


def test_fetch_serp_skips_direct_after_egress_marked_down(monkeypatch):
    """★一批 20 词不能每词都撞 4 次 6 秒超时：判定出口不通后，后续词直接走兜底。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_FALLBACK_READER", True)
    first = ss.fetch_serp("petroleum", session=_ReaderSession())
    assert first["via"] == "reader" and not first.get("skipped_direct")
    assert ss._STATE["direct_down_until"] > 0

    fake = _ReaderSession()
    second = ss.fetch_serp("another term", session=fake)
    assert second.get("skipped_direct") is True
    assert len(fake.urls) == 1 and "r.jina.ai" in fake.urls[0]  # 一次直连都没打


def test_fetch_serp_fast_path_falls_through_when_reader_also_down(monkeypatch):
    """记住的兜底出口失效时，要退回直连 4 发给它自愈机会，不能直接判死。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_FALLBACK_READER", True)
    ss._STATE["direct_down_until"] = float("inf")
    with pytest.raises(SourceUnavailable) as ei:
        ss.fetch_serp("petroleum", session=_ReaderSession(reader_down=True))
    assert len(ei.value.attempts) == 4         # 直连 4 发照常跑完
    assert "Reader 中转也连不上" in str(ei.value)


def test_fetch_interest_raises_source_unavailable(monkeypatch):
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    with pytest.raises(SourceUnavailable) as ei:
        st.fetch_interest("some term", session=_DownSession())
    assert ei.value.source == "google-trends"
    assert "连不上" in str(ei.value)
