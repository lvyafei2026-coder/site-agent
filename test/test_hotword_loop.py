# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""逐词循环图 hotword_loop 的图机制单测：条件边回环、limit 封顶、串行推进。

不重复 test_hotwords.py 的写回/降级语义（那些已被 run_batch 端到端覆盖），这里只钉
「循环由图驱动」这件事本身：节点拓扑、should_continue 的四种路由、limit 真能提前收口。
"""

from __future__ import annotations

from langgraph.graph import END

from src.app.graph import builder

from src.app.tools.adapters.terms import hotwords as hw
from src.app.tools.adapters.terms import hotword_batch, hotword_llm
from src.app.tools.adapters.terms import hotword_loop as hloop


def test_loop_graph_topology():
    """图恰有两个业务节点，且 analyze_one 有回环边指回 fetch_one。"""
    g = builder.get_hotword_graph().get_graph()
    nodes = set(g.nodes.keys())
    assert {"fetch_one", "analyze_one"} <= nodes
    # 条件边的分支里应含 END 与回环目标
    assert END in nodes


def test_should_continue_routes():
    base = {"limit": 5, "queue": [1, 2, 3], "cursor": 0,
            "loop_count": 0, "load_error": ""}
    assert hloop.should_continue(base) == "fetch_one"          # 有词、未达上限 → 回环
    assert hloop.should_continue({**base, "loop_count": 5}) == END   # 达上限
    assert hloop.should_continue({**base, "cursor": 3}) == END       # 词用完
    assert hloop.should_continue({**base, "load_error": "boom"}) == END  # KV 失败


def test_fetch_one_loads_once_then_pop_one(fake_kv, monkeypatch):
    """首次 fetch 读候选入队，之后每次只 pop 一个词，游标逐个推进。"""
    fake_kv.put_json("candidates", [{"term": f"t{i}"} for i in range(4)])
    monkeypatch.setattr(hw.scrape_serp, "fetch_serp", lambda *a, **k: {"engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest", lambda *a, **k: {"points": [], "summary": {}, "attempts": []})
    monkeypatch.setattr(hotword_llm, "_llm", lambda m: ("7) 结论：观望\n8) 信号标签：无", "fake"))

    s = hloop.HotwordLoopState(limit=10, kv=fake_kv, batch_run=None, batch_id="loop-test",
                               abort=hw._BatchAbort(), loaded=False, queue=[], cursor=0,
                               results=[], loop_count=0, examples=[])
    s1 = {**s, **hloop.fetch_one(s)}
    assert s1["loaded"] is True and len(s1["queue"]) == 4
    assert s1["cursor"] == 1 and s1["current"]["term"] == "t0"
    # 第二次 fetch：不再加载，只推进游标
    s2 = {**s1, **hloop.fetch_one(s1)}
    assert s2["cursor"] == 2 and s2["current"]["term"] == "t1"


def test_loop_stops_at_limit(fake_kv, monkeypatch):
    """给 8 个候选、limit=3，端到端只分析 3 个词——limit 是图路由的真实封顶。"""
    fake_kv.put_json("candidates", [{"term": f"term {i}"} for i in range(8)])
    monkeypatch.setattr(hw.scrape_serp, "fetch_serp", lambda *a, **k: {"engine": "duckduckgo", "items": [], "attempts": []})
    monkeypatch.setattr(hw.scrape_trend, "fetch_interest", lambda *a, **k: {"points": [], "summary": {}, "attempts": []})
    monkeypatch.setattr(hotword_llm, "_llm", lambda m: ("7) 结论：观望\n8) 信号标签：无", "fake"))

    out = hw.run_batch(limit=3)
    assert out["fetched"] == 3 and len(out["items"]) == 3
    # 未分析的词不会写 AI 缓存（只回写了 3 个）
    analyzed_keys = {hw.term_key(f"term {i}") for i in range(3)}
    left_keys = {hw.term_key(f"term {i}") for i in range(3, 8)}
    assert all(fake_kv.get_text(hw.AI_CACHE_PREFIX + k) for k in analyzed_keys)
    assert not any(fake_kv.get_text(hw.AI_CACHE_PREFIX + k) for k in left_keys)


def test_loop_kv_unreachable_short_circuits(monkeypatch):
    """候选池读取抛异常时，图经条件边直接 END，summarize 收口为 ok=False。"""
    class BoomKV:
        def list_keys(self, *a, **k):
            raise RuntimeError("kv down")

    monkeypatch.setattr(hotword_batch, "get_kv", lambda: BoomKV())
    out = hw.run_batch(limit=5)
    assert out["ok"] is False and "候选池读取失败" in out["error"]
    assert out["items"] == [] and out["fetched"] == 0
