# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用。详见 LICENSE.

"""提示词 D1 数据源回归（2026-11-17 迁移）：D1 优先 / 缓存 / 失败降级 / 默认版本。

不碰网络：注入 fake fetcher；配置开关用 monkeypatch 改 configured。
test_prompts.py 仍守本地文件的逐字 SHA；本文件守「读库链路 + 各种降级」。
"""

from __future__ import annotations

import pytest

from src.app.prompts.d1_source import D1PromptStore
from src.app.prompts.registry import PromptRegistry


FAKE_DATA = {
    "prompts": [
        {"task": "hotwords", "version": "v2", "content": "  D1 系统提示词 v2  "},
        {"task": "hotwords", "version": "v1", "content": "D1 老版本 v1"},
        {"task": "intent", "version": "v1", "content": "D1 意图分类"},
    ],
    "defaults": [
        {"task": "hotwords", "version": "v2"},
        {"task": "intent", "version": "v1"},
    ],
}


def make_store(data, *, ttl=60):
    calls = {"n": 0}

    def fetch(account, token, db):
        calls["n"] += 1
        if isinstance(data, Exception):
            raise data
        return data

    return D1PromptStore(fetcher=fetch, ttl=ttl), calls


def test_d1_content_and_default_version(monkeypatch):
    store, _ = make_store(FAKE_DATA)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    assert store.load("hotwords", "v2") == "D1 系统提示词 v2"      # 去首尾空白
    assert store.load("hotwords", "v999") is None                  # D1 无此版本 → 回退
    assert store.resolve_version("hotwords", "v1") == "v2"         # D1 默认覆盖 config
    assert store.resolve_version("unknown", "v1") == "v1"          # 无默认 → 用 config 值


def test_cache_one_fetch_per_ttl(monkeypatch):
    store, calls = make_store(FAKE_DATA, ttl=60)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    for _ in range(5):
        store.load("hotwords", "v2")
    assert calls["n"] == 1, f"60s 内应只拉一次全表，实际 {calls['n']} 次"
    store.clear()
    store.load("hotwords", "v2")
    assert calls["n"] == 2, "clear() 后应重新拉取"


def test_ttl_expiry_refetches(monkeypatch):
    store, calls = make_store(FAKE_DATA, ttl=0)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    store.load("hotwords", "v2")
    store.load("hotwords", "v2")
    assert calls["n"] == 2, "ttl=0 每次都应过期重拉"


def test_stale_on_error_then_empty(monkeypatch):
    # 先成功缓存，再失败 → 用旧快照
    data = {"ok": FAKE_DATA}
    store, calls = make_store(FAKE_DATA)

    def flaky(a, t, d):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("D1 down")
        return FAKE_DATA
    store = D1PromptStore(fetcher=flaky)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    assert store.load("hotwords", "v2") == "D1 系统提示词 v2"
    # 让缓存过期
    store._cache = (0.0, store._cache[1], store._cache[2])
    assert store.load("hotwords", "v2") == "D1 系统提示词 v2", "失败时应退回旧快照"
    assert calls["n"] == 2

    # 从未成功过 + 失败 → 空，交给注册表回退本地文件
    empty, _ = make_store(RuntimeError("D1 never up"))
    assert empty.load("hotwords", "v2") is None
    assert empty.resolve_version("hotwords", "v1") == "v1"


def test_not_configured_never_touches_fetch(monkeypatch):
    store, calls = make_store(FAKE_DATA)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: False))
    assert store.load("hotwords", "v2") is None
    assert store.resolve_version("hotwords", "v1") == "v1"
    assert calls["n"] == 0, "未配置 CF 凭据时绝不发网络请求"


def test_registry_prefers_d1_then_falls_back_to_file(monkeypatch, tmp_path):
    """注册表：D1 命中用 D1；D1 空则读本地 txt（逐字，含换行）。"""
    root = tmp_path
    (root / "hotwords").mkdir()
    (root / "hotwords" / "v2.txt").write_text("本地文件提示词 v2\n", encoding="utf-8")

    monkeypatch.setattr("src.app.prompts.registry.prompt_store", make_store(FAKE_DATA)[0])
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    reg = PromptRegistry()
    reg.root = root
    assert reg.load("hotwords", "v2") == "D1 系统提示词 v2"      # D1 优先

    monkeypatch.setattr("src.app.prompts.registry.prompt_store", make_store({"prompts": [], "defaults": []})[0])
    assert reg.load("hotwords", "v2") == "本地文件提示词 v2"      # D1 空 → 本地文件


def test_registry_default_version_from_d1(monkeypatch, tmp_path):
    root = tmp_path
    (root / "hotwords").mkdir()
    (root / "hotwords" / "v2.txt").write_text("本地 v2", encoding="utf-8")

    monkeypatch.setattr("src.app.prompts.registry.prompt_store", make_store(FAKE_DATA)[0])
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    reg = PromptRegistry()
    reg.root = root
    # 不传版本：resolve_version 读 D1 默认 v2，再加载 D1 的 v2 正文
    assert reg.load("hotwords") == "D1 系统提示词 v2"
