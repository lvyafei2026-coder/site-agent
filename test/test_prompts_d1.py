# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；详见 LICENSE.

"""提示词 D1 数据源回归（2026-11-17 迁移）：D1 优先 / 缓存 / 失败降级 / 默认版本。

不碰网络：测试把 store 的取数函数换成桩；配置开关用 monkeypatch 改 configured。
本地 versions/*.txt 已删除：D1 读不到且无旧快照时注册表抛错（不再回退文件）。
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


def make_store(monkeypatch, data, *, ttl=60):
    """生产 store 固定读 D1；测试把取数函数和 TTL 直接换成桩。"""
    calls = {"n": 0}

    def fetch(account, token, db):
        calls["n"] += 1
        if isinstance(data, Exception):
            raise data
        return data

    store = D1PromptStore()
    monkeypatch.setattr(store, "_fetcher", fetch)
    monkeypatch.setattr(store, "_ttl", ttl)
    return store, calls


def test_d1_content_and_default_version(monkeypatch):
    store, _ = make_store(monkeypatch, FAKE_DATA)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    assert store.load("hotwords", "v2") == "D1 系统提示词 v2"      # 去首尾空白
    assert store.load("hotwords", "v999") is None                  # D1 无此版本
    assert store.resolve_version("hotwords", "v1") == "v2"         # D1 默认覆盖 config
    assert store.resolve_version("unknown", "v1") == "v1"          # 无默认 → 用 config 值
    assert store.versions("hotwords") == ["v1", "v2"]
    assert store.versions("unknown") == []


def test_cache_one_fetch_per_ttl(monkeypatch):
    store, calls = make_store(monkeypatch, FAKE_DATA, ttl=60)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    for _ in range(5):
        store.load("hotwords", "v2")
    assert calls["n"] == 1, f"60s 内应只拉一次全表，实际 {calls['n']} 次"
    store._cache = None
    store.load("hotwords", "v2")
    assert calls["n"] == 2, "清空缓存后应重新拉取"


def test_ttl_expiry_refetches(monkeypatch):
    store, calls = make_store(monkeypatch, FAKE_DATA, ttl=0)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    store.load("hotwords", "v2")
    store.load("hotwords", "v2")
    assert calls["n"] == 2, "ttl=0 每次都应过期重拉"


def test_stale_on_error_then_empty(monkeypatch):
    # 先成功缓存，再失败 → 用旧快照
    calls = {"n": 0}

    def flaky(a, t, d):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("D1 down")
        return FAKE_DATA

    store = D1PromptStore()
    monkeypatch.setattr(store, "_fetcher", flaky)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    assert store.load("hotwords", "v2") == "D1 系统提示词 v2"
    # 让缓存过期
    store._cache = (0.0, store._cache[1], store._cache[2])
    assert store.load("hotwords", "v2") == "D1 系统提示词 v2", "失败时应退回旧快照"
    assert calls["n"] == 2

    # 从未成功过 + 失败 → 空，注册表据此抛错
    empty, _ = make_store(monkeypatch, RuntimeError("D1 never up"))
    assert empty.load("hotwords", "v2") is None
    assert empty.resolve_version("hotwords", "v1") == "v1"


def test_not_configured_never_touches_fetch(monkeypatch):
    store, calls = make_store(monkeypatch, FAKE_DATA)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: False))
    assert store.load("hotwords", "v2") is None
    assert store.resolve_version("hotwords", "v1") == "v1"
    assert store.versions("hotwords") == []
    assert calls["n"] == 0, "未配置 CF 凭据时绝不发网络请求"


def test_registry_prefers_d1(monkeypatch):
    store, _ = make_store(monkeypatch, FAKE_DATA)
    monkeypatch.setattr("src.app.prompts.registry.prompt_store", store)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    reg = PromptRegistry()
    assert reg.load("hotwords", "v2") == "D1 系统提示词 v2"      # D1 命中
    assert reg.available_versions("hotwords") == ["v1", "v2"]


def test_registry_missing_version_falls_back_to_default(monkeypatch):
    store, _ = make_store(monkeypatch, FAKE_DATA)
    monkeypatch.setattr("src.app.prompts.registry.prompt_store", store)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    reg = PromptRegistry()
    # 请求不存在的 v999 → 回退 hotwords 的 D1 默认 v2
    assert reg.load("hotwords", "v999") == "D1 系统提示词 v2"


def test_registry_raises_when_d1_has_nothing(monkeypatch):
    """D1 空且无旧快照 → 明确抛错，不再静默降级（避免空提示词让 LLM 乱答）。"""
    store, _ = make_store(monkeypatch, {"prompts": [], "defaults": []})
    monkeypatch.setattr("src.app.prompts.registry.prompt_store", store)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    reg = PromptRegistry()
    with pytest.raises(RuntimeError, match="/rules/prompts"):
        reg.load("hotwords", "v2")


def test_registry_default_version_from_d1(monkeypatch):
    store, _ = make_store(monkeypatch, FAKE_DATA)
    monkeypatch.setattr("src.app.prompts.registry.prompt_store", store)
    monkeypatch.setattr(D1PromptStore, "configured", staticmethod(lambda: True))
    reg = PromptRegistry()
    # 不传版本：resolve_version 读 D1 默认 v2，再加载 D1 的 v2 正文
    assert reg.load("hotwords") == "D1 系统提示词 v2"
