# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""FastAPI 入站契约：/run 与 /health。"""

from fastapi.testclient import TestClient

from src.app.api.routes import api, health
from src.app.graph import run_workflow


def test_health():
    assert health()["status"] == "ok"


def test_run_workflow(fake):
    r = run_workflow(input_text="看看新热词", intent="word_plan_page")
    assert r["intent"] == "word_plan_page"
    assert "output" in r


def test_run_returns_langsmith_link_fields(fake, monkeypatch):
    """/run 带回 langsmith{enabled,project,url} 供页面回链，且绝不带 key。"""
    from src.app.config import settings

    monkeypatch.setattr(settings, "langsmith_api_key", "lsv2_pt_secret")
    monkeypatch.setattr(settings, "langsmith_project_url",
                        "https://smith.langchain.com/o/acme/projects/p/1")
    r = run_workflow(input_text="看看热词", intent="word_plan_page")
    assert set(r["langsmith"]) == {"enabled", "project", "url"}
    assert r["langsmith"]["project"] == settings.langsmith_project
    assert r["langsmith"]["url"].endswith("/projects/p/1")
    assert "lsv2_pt_secret" not in str(r["langsmith"])


def test_http_run_and_health(fake):
    client = TestClient(api)
    assert client.get("/health").json()["status"] == "ok"
    resp = client.post("/run", json={"input_text": "复盘数据", "intent": "review_metrics"})
    assert resp.status_code == 200
    assert resp.json()["intent"] == "review_metrics"


def test_http_validation_error():
    client = TestClient(api)
    assert client.post("/run", json={}).status_code == 422


def test_analyze_reanalyzes_named_term(fake_kv, monkeypatch):
    """POST /analyze 点名重新分析：按词名回取（不看剔除）+ 用缓存证据 + 按新结论回写。"""
    from src.app.tools.adapters.cloudflare import kv as kvk
    from src.app.utils import llm_io

    term = "november full moon"
    key = kvk.term_key(term)
    fake_kv.put_json("saved", [{"key": key, "term": term, "status": "wip",
                                "traffic": 1200, "hints": ["工具查询"]}])
    # ★线上事故形态：这个词早先被 AI 判过不做，改判「做」时必须把不做记录一并清掉，
    #   否则候选页默认隐藏判不做的词，状态再对也看不见。
    fake_kv.put_json("rejected", [{"key": key, "term": term,
                                   "reason": "AI 判定不做", "source": "ai"}])
    fake_kv.put_json(kvk.SERP_PREFIX + key,
                     {"engine": "duckduckgo", "items": [],
                      "competition": {"score": 1, "level": "low"}})
    fake_kv.put_json(kvk.TREND_PREFIX + key, {"points": [], "summary": {}})
    fake_kv.put_text(kvk.AI_CACHE_PREFIX + key, "旧方案正文")   # 已分析过也要能重跑
    monkeypatch.setattr(llm_io, "complete",
                        lambda m, **_: ("7) 结论：做\n8) 信号标签：工具查询", "fake"))
    # D1 写桩掉，只验调用参数；归类 LLM 桩成「无结果」→ 由别名规则兜底命中。
    from src.app.services import page_build_service as pbs
    from src.app.services.extraction_service import extraction_service
    from src.app.tools.adapters.cloudflare.sql import Sql

    monkeypatch.setattr(extraction_service, "extract",
                        lambda text, intent, versions=None: {"target_site": ""})
    updates = []
    monkeypatch.setattr(pbs.database, "execute",
                        lambda sql, params: updates.append((sql, params)))

    data = TestClient(api).post("/analyze", json={"term": term}).json()
    assert data["ok"] is True and data["fetched"] == 1 and data["analyzed"] == 1
    item = data["items"][0]
    assert item["term"] == term and item["verdict"] == "做" and item["status"] == "todo"
    # 正文覆盖旧方案，状态按新结论回写（与批量分析同一套 write_result）
    assert "结论：做" in fake_kv.get_text(kvk.AI_CACHE_PREFIX + key)
    assert fake_kv.get_json("saved")[0]["status"] == "todo"
    # 改判互清：AI 的不做记录被清掉，这个词不会再被「隐藏判不做」藏起来
    assert fake_kv.get_json("rejected", []) == []
    # ★专属收尾：新正文里的 Beaver Moon 命中 beaver-moon 别名 → 回写待建行 site
    assert data["siteRetarget"]["site"] == "beaver-moon"
    assert data["siteRetarget"]["changed"] is True
    assert updates == [(Sql.RETARGET_PLANNED_SITE, ["beaver-moon", term])]


def test_analyze_unknown_term_reports_error(fake_kv, monkeypatch):
    """词库里找不到这个词 → 不发 LLM，直接 ok=False 收口。"""
    from src.app.utils import llm_io

    monkeypatch.setattr(llm_io, "complete", lambda *a, **k: 1 / 0)   # 被调用就炸
    data = TestClient(api).post("/analyze", json={"term": "no such term"}).json()
    assert data["ok"] is False and "找不到" in data["error"] and data["items"] == []


def _stub_retarget(monkeypatch):
    """站点重判的两条外部 IO 都桩掉：归类 LLM 无结果（走别名规则）、D1 UPDATE 不落库。"""
    from src.app.services import page_build_service as pbs
    from src.app.services.extraction_service import extraction_service

    monkeypatch.setattr(extraction_service, "extract",
                        lambda text, intent, versions=None: {"target_site": ""})
    monkeypatch.setattr(pbs.database, "execute", lambda sql, params: None)


def test_analyze_skip_clears_pending_saved(fake_kv, monkeypatch):
    """改判「不做」：saved 里未开工（saved/todo）的行被清，不做清单记 AI 来源。"""
    from src.app.tools.adapters.cloudflare import kv as kvk
    from src.app.utils import llm_io

    term = "november full moon"
    key = kvk.term_key(term)
    fake_kv.put_json("saved", [{"key": key, "term": term, "status": "todo"}])
    fake_kv.put_json(kvk.SERP_PREFIX + key, {"engine": "duckduckgo", "items": []})
    fake_kv.put_json(kvk.TREND_PREFIX + key, {"points": [], "summary": {}})
    monkeypatch.setattr(llm_io, "complete",
                        lambda m, **_: ("7) 结论：不做\n8) 信号标签：无", "fake"))
    _stub_retarget(monkeypatch)

    data = TestClient(api).post("/analyze", json={"term": term}).json()
    assert data["items"][0]["status"] == "rejected"
    assert fake_kv.get_json("saved", []) == []
    assert [r["source"] for r in fake_kv.get_json("rejected")] == ["ai"]


def test_analyze_skip_keeps_started_saved(fake_kv, monkeypatch):
    """改判「不做」不抹人工进度：wip/live/parked 的跟进行保留，只写不做清单。"""
    from src.app.tools.adapters.cloudflare import kv as kvk
    from src.app.utils import llm_io

    term = "november full moon"
    key = kvk.term_key(term)
    fake_kv.put_json("saved", [{"key": key, "term": term, "status": "wip"}])
    fake_kv.put_json(kvk.SERP_PREFIX + key, {"engine": "duckduckgo", "items": []})
    fake_kv.put_json(kvk.TREND_PREFIX + key, {"points": [], "summary": {}})
    monkeypatch.setattr(llm_io, "complete",
                        lambda m, **_: ("7) 结论：不做\n8) 信号标签：无", "fake"))
    _stub_retarget(monkeypatch)

    data = TestClient(api).post("/analyze", json={"term": term}).json()
    assert data["items"][0]["status"] == "rejected"
    assert fake_kv.get_json("saved")[0]["status"] == "wip"
    assert fake_kv.get_json("rejected")[0]["source"] == "ai"


def _wait_hotwords_idle(timeout=5.0):
    """等后台那一轮收口（done / error），返回最后一次快照。"""
    import time

    from src.app.observability.hotword_progress import hotword_progress

    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = hotword_progress.snapshot()
        if snap["status"] != "running":
            return snap
        time.sleep(0.01)
    raise AssertionError("后台轮次没有收口")


def test_hotwords_starts_in_background_and_reports_progress(fake, monkeypatch):
    """候选池分析是独立入口且异步：POST 立即返回，进度与终态走 GET /hotwords/status。"""
    import src.app.api.routes as routes

    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        return {"ok": True, "analyzed": 3, "failed": 0, "items": []}

    monkeypatch.setattr(routes, "run_hotwords", fake_run)
    client = TestClient(api)

    r = client.post("/hotwords", json={"limit": 5, "prompt_version": "v2"})
    assert r.status_code == 200 and r.json() == {"ok": True, "started": True, "limit": 5}
    snap = _wait_hotwords_idle()
    assert snap["status"] == "done" and snap["summary"]["analyzed"] == 3
    assert seen == {"limit": 5, "prompt_version": "v2", "evidence": None, "post": None}

    # 步骤名单原样透传给 run_hotwords：白名单校验在 state 工厂，入口层不重复一遍
    r = client.post("/hotwords", json={"evidence": ["trend"], "post": []})
    assert r.status_code == 200
    _wait_hotwords_idle()
    assert seen["evidence"] == ["trend"] and seen["post"] == []

    # limit 入口按上限收口：999 → 100（run_hotwords 里还有同一常量收一次）
    r = client.post("/hotwords", json={"limit": 999})
    assert r.json()["limit"] == 100
    _wait_hotwords_idle()
    assert seen["limit"] == 100

    # /run 仍走主图，不再因 intent=analyze_hotwords 分流到热词图
    r2 = client.post("/run", json={"input_text": "分析热词", "intent": "analyze_hotwords"})
    assert r2.status_code == 200 and r2.json()["intent"] == "analyze_hotwords"
    assert "analyzed" not in r2.json()   # 没有热词结果字段，说明走的是主图


def test_hotwords_rejects_overlapping_run(fake, monkeypatch):
    """一轮没跑完时再 POST → 409：进度账本只有一份，避免两轮互相覆盖。"""
    import threading

    import src.app.api.routes as routes

    gate = threading.Event()

    def slow(**kw):
        gate.wait(5)
        return {"ok": True}

    monkeypatch.setattr(routes, "run_hotwords", slow)
    client = TestClient(api)
    try:
        assert client.post("/hotwords", json={"limit": 1}).status_code == 200
        r = client.post("/hotwords", json={"limit": 1})
        assert r.status_code == 409 and "在跑" in r.json()["detail"]
    finally:
        gate.set()
        _wait_hotwords_idle()


def test_llm_quota_endpoint(monkeypatch):
    """/llm-quota 透传 check_quota 结果；未配 key 时 ok=False 且不抛异常。"""
    import src.app.llm.quota as quota_mod

    client = TestClient(api)

    monkeypatch.setattr(quota_mod, "LLM_API_KEY", "")   # 不依赖本机 .env
    r = client.get("/llm-quota").json()
    assert r["ok"] is False
    assert "LLM_API_KEY" in r["reason"]

    import src.app.api.routes as routes
    fake = {"ok": True, "model": "deepseek-flash", "endpoint": "api.deepseek.com",
            "quota": {"x-ratelimit-remaining-requests": "149"}, "checked_at": "t"}
    monkeypatch.setattr(routes, "check_quota", lambda: fake)
    assert client.get("/llm-quota").json() == fake
