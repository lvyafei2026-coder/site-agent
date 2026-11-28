# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""hotwords 适配器的纯逻辑测试：KV 读写、SERP/Trend 纯函数、整理写侧、候选快照。

全部走内存 FakeKV + monkeypatch 外部 IO，不碰 Cloudflare / Google / DuckDuckGo / OpenAI。
KV 实现在 ../cloudflare/kv.py（全项目唯一 KV 模块），这些模块是
「agent 直读共享 KV、去除 console/radar 接口依赖」改造后的核心，必须离线可测。

生产代码不提供任何测试注入口：HTTP 走 requests、KV 走 get_kv()，测试侧在这里
把假响应 / 假 session 接到真实的 requests 调用点上。
"""

import json
import urllib.parse

import pytest
import requests as _requests

from src.app.tools.adapters.hotwords import scrape_serp as ss
from src.app.tools.adapters.hotwords import scrape_trend as st
from src.app.utils.source_errors import SourceUnavailable
from src.app.utils.term_key import term_key


@pytest.fixture(autouse=True)
def _reset_serp_egress_state():
    """SERP「直连出口不通」是进程级状态，每个用例前后清零，避免跨用例污染。"""
    ss._STATE["direct_down_until"] = 0.0
    yield
    ss._STATE["direct_down_until"] = 0.0


# ---- 测试侧 IO 适配：生产只走 requests / get_kv，这里把假对象接上去 ----
_REAL_SESSION = _requests.Session
_SESSION = {"current": None}


@pytest.fixture(autouse=True)
def _session_egress(monkeypatch):
    """生产 fetch_serp/fetch_interest 里的 requests.Session() 接到用例指定的假 session。"""
    _SESSION["current"] = None
    monkeypatch.setattr(_requests, "Session",
                        lambda: _SESSION["current"] or _REAL_SESSION())
    yield
    _SESSION["current"] = None


def fetch_serp(term, *, session, **kw):
    _SESSION["current"] = session
    return ss.fetch_serp(term, **kw)


def fetch_interest(term, *, session, **kw):
    _SESSION["current"] = session
    return st.fetch_interest(term, **kw)


def fake_cf_http(monkeypatch, transport):
    """把假 HTTP 响应接到 CloudflareKV._request 的 requests 调用点上。

    transport 签名沿用旧测试习惯：(method, url, headers, body) -> (status, text)。
    生产 _request 用 requests.request / requests.get，这里把 params 还原成带 query 的 URL
    再转给 transport，保证 URL 编码 / TTL 参数断言与线上路径一致。
    """
    class Resp:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text

    def full_url(url, params):
        return url + ("?" + urllib.parse.urlencode(params) if params else "")

    def fake_request(method, url, params=None, headers=None, data=None, timeout=None):
        status, text = transport(method, full_url(url, params), headers, data)
        return Resp(status, text)

    def fake_get(url, params=None, headers=None, timeout=None):
        status, text = transport("GET", full_url(url, params), headers, None)
        return Resp(status, text)

    monkeypatch.setattr(kvmod.requests, "request", fake_request)
    monkeypatch.setattr(kvmod.requests, "get", fake_get)


# ---------------------------------------------------------------------------
# cloudflare/kv.py：CloudflareKV 的键常量 / term_key / HTTP 请求构造
# ---------------------------------------------------------------------------
def test_term_key_normalizes():
    assert term_key("  Free   Invoice CALCULATOR  ") == "free invoice calculator"


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
# wordlib.py：整理写侧（与 console/src/store.js 同口径）
# ---------------------------------------------------------------------------
def test_fetch_serp_raises_source_unavailable_on_connect_failure(monkeypatch):
    """连不上 DDG（Render 出口实测）→ 源级不可用，上层中止整批。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    with pytest.raises(SourceUnavailable) as ei:
        fetch_serp("cba housing market downturn outlook", session=_DownSession())
    assert ei.value.source == "duckduckgo"
    assert "连不上" in str(ei.value)
    assert len(ei.value.attempts) == 4          # 4 发都记了账
    assert all(a["status"] == 0 for a in ei.value.attempts)


def test_fetch_serp_raises_source_unavailable_on_challenge_pages(monkeypatch):
    """被判定异常时仍可能回 202 挑战页 → 仍是源级不可用，不能当「无结果」。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    with pytest.raises(SourceUnavailable) as ei:
        fetch_serp("some term", session=_ChallengeSession())
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
    out = fetch_serp("example term", session=_MinimalHeaderSession())
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
    out = fetch_serp("example term", session=fake)
    assert fake.seen == ["html.duckduckgo.com", "duckduckgo.com"]
    assert [x["domain"] for x in out["items"]] == ["example.com", "other.org", "plain.example.net"]
    assert out["engine"] == "duckduckgo"
    assert len({s["host"] for s in ss.PLAN}) == 4


def test_fetch_serp_connect_failure_reports_hosts_and_error_kind(monkeypatch):
    """★Render 出口 4 发全挂时，报错要直接说清「哪些 host、什么异常」，
    别再甩一长串被 UI 截断的 HTTPSConnectionPool 文本。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    with pytest.raises(SourceUnavailable) as ei:
        fetch_serp("petroleum", session=_DownSession())
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
    fetch_serp("x", session=plain)
    assert "proxies" not in plain.kwargs[0]

    monkeypatch.setattr(ss, "SERP_HTTPS_PROXY", "http://127.0.0.1:8899")
    proxied = _HtmlSession()
    fetch_serp("x", session=proxied)
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
    out = fetch_serp("petroleum", session=fake)
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
        fetch_serp("petroleum", session=fake)
    assert not any("r.jina.ai" in u for u in fake.urls)


def test_fetch_serp_reports_reader_failure_when_both_paths_down(monkeypatch):
    """两条出口都挂 → 诊断要点名 Reader，避免又回到「是不是请求头不对」的老猜测。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_FALLBACK_READER", True)
    fake = _ReaderSession(reader_down=True)
    with pytest.raises(SourceUnavailable) as ei:
        fetch_serp("petroleum", session=fake)
    assert "Reader 中转也连不上" in str(ei.value)
    assert "ConnectTimeout" in str(ei.value)


def test_fetch_serp_skips_direct_after_egress_marked_down(monkeypatch):
    """★一批 20 词不能每词都撞 4 次 6 秒超时：判定出口不通后，后续词直接走兜底。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_FALLBACK_READER", True)
    first = fetch_serp("petroleum", session=_ReaderSession())
    assert first["via"] == "reader" and not first.get("skipped_direct")
    assert ss._STATE["direct_down_until"] > 0

    fake = _ReaderSession()
    second = fetch_serp("another term", session=fake)
    assert second.get("skipped_direct") is True
    assert len(fake.urls) == 1 and "r.jina.ai" in fake.urls[0]  # 一次直连都没打


def test_fetch_serp_fast_path_falls_through_when_reader_also_down(monkeypatch):
    """记住的兜底出口失效时，要退回直连 4 发给它自愈机会，不能直接判死。"""
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_FALLBACK_READER", True)
    ss._STATE["direct_down_until"] = float("inf")
    with pytest.raises(SourceUnavailable) as ei:
        fetch_serp("petroleum", session=_ReaderSession(reader_down=True))
    assert len(ei.value.attempts) == 4         # 直连 4 发照常跑完
    assert "Reader 中转也连不上" in str(ei.value)


JINA_MD = """Title: beaver moon at DuckDuckGo

## [Full Beaver Moon](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.almanac.com%2Ffull%2Dmoon&rut=abc)

[![Image 1](https://external-content.duckduckgo.com/ip3/x.ico)](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.almanac.com%2Ffull%2Dmoon&rut=abc)

[摘要行不算结果](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.almanac.com%2Ffull%2Dmoon&rut=abc)

## [Where to watch](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.space.com%2Fmoon&rut=def)
"""


def test_parse_jina_markdown_results():
    """Reader 正常模式的 markdown 只取 ## 结果行，uddg 解码后与 parse_ddg 同口径。"""
    items = ss.parse_jina_markdown(JINA_MD, 10)
    assert [x["domain"] for x in items] == ["almanac.com", "space.com"]
    assert items[0]["title"] == "Full Beaver Moon"
    assert items[0]["url"] == "https://www.almanac.com/full-moon"


def test_reader_challenge_falls_back_to_markdown(monkeypatch):
    """线上实证：原文模式带回 DDG 202 challenge，正常 markdown 模式能拿到真结果。"""
    from types import SimpleNamespace

    import requests as rq

    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "SERP_FALLBACK_READER", True)
    calls: list[dict] = []

    class FakeSession:
        def get(self, url, **kwargs):
            if "r.jina.ai" not in url:
                raise rq.exceptions.ConnectTimeout("direct down")
            calls.append(kwargs.get("headers", {}))
            if len(calls) == 1:
                return SimpleNamespace(status_code=202,
                                       text="<title>DuckDuckGo</title> are you a robot")
            return SimpleNamespace(status_code=200, text=JINA_MD)

    out = fetch_serp("beaver moon", session=FakeSession())
    assert out["via"] == "reader"
    assert [x["domain"] for x in out["items"]] == ["almanac.com", "space.com"]
    assert len(calls) == 2
    assert "x-respond-with" in calls[0] and "x-respond-with" not in calls[1]


def test_fetch_interest_raises_source_unavailable(monkeypatch):
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    with pytest.raises(SourceUnavailable) as ei:
        fetch_interest("some term", session=_DownSession())
    assert ei.value.source == "google-trends"
    assert "连不上" in str(ei.value)


# ---------------------------------------------------------------------------
# 整表写入的并发安全：进程内一把锁串行「读-改-写」，★不做写后读回重试
# ---------------------------------------------------------------------------

