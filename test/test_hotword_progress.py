# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词线进度账本：占位 / 逐词计数 / 收口 / 收口后可再发起。"""

from src.app.observability.hotword_progress import DONE, ERROR, IDLE, HotwordProgress


def test_progress_lifecycle():
    p = HotwordProgress()
    assert p.snapshot()["status"] == IDLE
    assert p.start(3) is True
    assert p.start(3) is False            # 同一时刻只允许一轮

    p.report([{"term": "a"}, {"term": "b", "error": "boom"}])
    snap = p.snapshot()
    assert (snap["done"], snap["analyzed"], snap["failed"]) == (2, 1, 1)
    assert snap["term"] == "b" and snap["elapsedMs"] >= 0

    p.finish({"ok": True})
    assert p.snapshot()["status"] == DONE
    assert p.start(1) is True             # 上一轮收口后可再发起


def test_report_is_noop_when_idle():
    p = HotwordProgress()
    p.report([{"term": "a"}])
    assert p.snapshot()["status"] == IDLE


def test_fail_records_error():
    p = HotwordProgress()
    p.start(1)
    p.fail("boom")
    snap = p.snapshot()
    assert snap["status"] == ERROR and snap["error"] == "boom"
