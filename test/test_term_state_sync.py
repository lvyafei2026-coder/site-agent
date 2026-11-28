# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""agent_pages 与 radar_term_status 的 D1 状态同步。"""

from src.app.enums import CurationStatus, PageStatus
from src.app.tools.adapters.hotwords import state_sync


def test_norm_term_folds_hyphen_and_slug():
    assert state_sync.norm_term("  Free-Invoice_Generator ") == "free invoice generator"


def test_project_never_downgrades():
    updates, changes = state_sync.project(
        [
            {"keyword": "a-term", "status": PageStatus.PLANNED.value},
            {"keyword": "b-term", "status": PageStatus.VERIFIED.value},
            {"keyword": "c-term", "status": PageStatus.PLANNED.value},
        ],
        [
            {"term_id": 1, "term_key": "a term", "term": "a term", "status": "todo"},
            {"term_id": 2, "term_key": "b term", "term": "b term", "status": "wip"},
            {"term_id": 3, "term_key": "c term", "term": "c term", "status": "parked"},
        ],
    )
    assert updates == [{"term_id": 1, "status": "wip"}, {"term_id": 2, "status": "live"}]
    assert [item["to"] for item in changes] == ["wip", "live"]


def test_sync_updates_only_existing_term_status(fake_d1):
    todo_id = fake_d1.seed_term("free invoice generator")
    parked_id = fake_d1.seed_term("cold brew ratio")
    fake_d1.seed_status(todo_id, status="todo")
    fake_d1.seed_status(parked_id, status="parked")
    fake_d1.execute(
        """INSERT INTO agent_pages (site,keyword,slug,status,created_at)
           VALUES ('invoices','free invoice generator','free-invoice-generator','planned',1),
                  ('coffee','cold brew ratio','cold-brew-ratio','verified',1)"""
    )
    result = state_sync.sync()
    todo = fake_d1.query("SELECT status FROM radar_term_status WHERE term_id=?", [todo_id])[0]
    parked = fake_d1.query("SELECT status FROM radar_term_status WHERE term_id=?", [parked_id])[0]
    assert result["updated"] == 1
    assert todo["status"] == CurationStatus.WIP.value
    assert parked["status"] == CurationStatus.PARKED.value


def test_mark_uses_the_same_d1_rule(fake_d1):
    term_id = fake_d1.seed_term("free invoice generator")
    fake_d1.seed_status(term_id, status="todo")
    result = state_sync.mark(["free invoice generator"], CurationStatus.WIP.value)
    row = fake_d1.query("SELECT status FROM radar_term_status WHERE term_id=?", [term_id])[0]
    assert result["updated"] == 1
    assert row["status"] == CurationStatus.WIP.value


def test_sync_propagates_d1_read_failure(fake_d1, monkeypatch):
    monkeypatch.setattr(state_sync.database, "query",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("D1 read failed")))
    try:
        state_sync.sync()
    except RuntimeError as exc:
        assert "D1 read failed" in str(exc)
    else:
        raise AssertionError("D1 read failure must not be hidden")
