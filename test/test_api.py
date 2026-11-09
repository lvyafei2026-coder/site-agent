# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""FastAPI 入站契约：/run 与 /health。"""

from fastapi.testclient import TestClient

from src.app.api.routes import api, health, invoke_workflow


def test_health():
    assert health()["status"] == "ok"


def test_invoke_workflow(fake):
    r = invoke_workflow({"input_text": "看看新热词", "intent": "select_pages"})
    assert r["intent"] == "select_pages"
    assert "output" in r


def test_run_returns_langsmith_link_fields(fake, monkeypatch):
    """/run 带回 langsmith{enabled,project,url} 供页面回链，且绝不带 key。"""
    from src.app.config import settings

    monkeypatch.setattr(settings, "langsmith_api_key", "lsv2_pt_secret")
    monkeypatch.setattr(settings, "langsmith_project_url",
                        "https://smith.langchain.com/o/acme/projects/p/1")
    r = invoke_workflow({"input_text": "看看热词", "intent": "select_pages"})
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
