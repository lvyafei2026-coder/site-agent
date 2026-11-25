# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；详见 LICENSE.

"""热词状态闭环：台账进度 → saved 跟进状态。

测试只替换取数点（state_sync.get_kv / state_sync.database），不传注入参数给生产函数。
"""

import json

import src.app.tools.adapters.hotwords.state_sync as ss
from src.app.tools.adapters.hotwords import state_sync


def _saved(monkeypatch, fake_kv, rows):
    """给 FakeKV 塞 saved 表；patch state_sync 自己那条 get_kv 引用。"""
    fake_kv.store["saved"] = json.dumps(rows, ensure_ascii=False)
    monkeypatch.setattr(state_sync, "get_kv", lambda: fake_kv)


def test_norm_term_folds_hyphen_and_slug(monkeypatch, fake_kv):
    assert ss.norm_term("is-it-black-friday-today") == ss.norm_term("Is It Black Friday Today")
    assert ss.norm_term("when_is_next_friday") == ss.norm_term("when is next friday")


def test_project_planned_to_wip_and_deployed_to_live():
    saved = [
        {"term": "when is the next friday the 13th", "status": "todo"},
        {"term": "is it black friday today", "status": "todo"},
        {"term": "unrelated term", "status": "todo"},
    ]
    rows = [
        {"keyword": "when is the next friday the 13th", "status": "planned"},
        {"keyword": "is-it-black-friday-today", "status": "deployed"},  # slug 形态
    ]
    out, changes = ss.project(rows, saved)
    by_term = {x["term"]: x["status"] for x in out}
    assert by_term["when is the next friday the 13th"] == "wip"
    assert by_term["is it black friday today"] == "live"
    assert by_term["unrelated term"] == "todo"   # 台账里没有 → 不动
    assert len(changes) == 2


def test_project_never_downgrades(monkeypatch, fake_kv):
    saved = [
        {"term": "a term", "status": "live"},
        {"term": "b term", "status": "wip"},
        {"term": "c term", "status": "parked"},   # 人工搁置不自动改
    ]
    rows = [
        {"keyword": "a term", "status": "planned"},   # live 不退回 wip
        {"keyword": "b term", "status": "todo"},       # todo 不覆盖 wip（台账状态无效，也不算升）
        {"keyword": "c term", "status": "deployed"},
    ]
    out, changes = ss.project(rows, saved)
    assert [x["status"] for x in out] == ["live", "wip", "parked"]
    assert changes == []


def test_project_skips_failed_and_unknown_status():
    saved = [{"term": "a term", "status": "todo"}, {"term": "b term", "status": "todo"}]
    rows = [
        {"keyword": "a term", "status": "failed"},
        {"keyword": "b term", "status": "weird"},
    ]
    out, changes = ss.project(rows, saved)
    assert changes == []
    assert [x["status"] for x in out] == ["todo", "todo"]


def test_project_does_not_create_new_words():
    out, changes = ss.project([{"keyword": "never saved", "status": "deployed"}],
                              [{"term": "other", "status": "todo"}])
    assert len(out) == 1 and out[0]["term"] == "other"
    assert changes == []


def test_project_prefers_later_status_across_rows():
    saved = [{"term": "a term", "status": "todo"}]
    rows = [
        {"keyword": "a term", "status": "planned"},
        {"keyword": "a-term", "status": "deployed"},   # 同词的 slug 形态，取更靠后
    ]
    out, changes = ss.project(rows, saved)
    assert out[0]["status"] == "live"
    assert changes[0]["to"] == "live"


def test_sync_writes_back_only_changed_words(monkeypatch, fake_kv):
    _saved(monkeypatch, fake_kv, [
        {"term": "a term", "status": "todo"},
        {"term": "b term", "status": "todo"},
    ])
    rows = [{"keyword": "a term", "status": "planned"}]
    monkeypatch.setattr(state_sync, "read_ledger", lambda: rows)

    out = ss.sync()
    assert out["ok"] is True and out["updated"] == 1
    saved = json.loads(fake_kv.store["saved"])
    assert saved[0]["status"] == "wip"
    assert saved[1]["status"] == "todo"

    before = fake_kv.writes
    out2 = ss.sync()          # 幂等：第二次无变化，不再写 KV
    assert out2["updated"] == 0
    assert fake_kv.writes == before


def test_sync_reports_kv_write_failure(monkeypatch, fake_kv):
    _saved(monkeypatch, fake_kv, [{"term": "a term", "status": "todo"}])
    fake_kv.fail_keys.add("saved")
    monkeypatch.setattr(state_sync, "read_ledger",
                        lambda: [{"keyword": "a term", "status": "deployed"}])

    out = ss.sync()
    assert out["ok"] is False and out["updated"] == 0
    assert out["changes"][0]["to"] == "live"


def test_mark_uses_production_rule_without_ledger(monkeypatch, fake_kv):
    _saved(monkeypatch, fake_kv, [{"term": "a term", "status": "todo"}])
    out = ss.mark(["a term"], "live")
    assert out["ok"] is True and out["updated"] == 1
    assert json.loads(fake_kv.store["saved"])[0]["status"] == "live"


def test_read_ledger_degrades_to_empty(monkeypatch):
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("D1 down")

    monkeypatch.setattr(state_sync, "database", Boom())
    assert ss.read_ledger() == []


def test_sync_term_status_tool_is_registered():
    from src.app.tools.registry import tool_registry
    tool = tool_registry.get("sync_term_status")
    assert tool is not None
    assert tool.kind == "ledger"
