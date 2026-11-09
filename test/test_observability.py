# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""LangSmith 开关：关着必须零开销且不污染环境，开着才包装函数与 LLM 客户端。"""

from __future__ import annotations

import os

import pytest

from src.app.config import settings
from src.app.observability import langsmith as ls


@pytest.fixture
def tracing(monkeypatch):
    """按需临时打开/关闭追踪，用例结束自动还原。"""

    def _set(enabled: bool, key: str = "lsv2_pt_test"):
        monkeypatch.setattr(settings, "langsmith_tracing", enabled)
        monkeypatch.setattr(settings, "langsmith_api_key", key)
        ls.configure_langsmith()

    yield _set
    monkeypatch.setattr(settings, "langsmith_tracing", False)
    monkeypatch.setattr(settings, "langsmith_api_key", "")
    ls.configure_langsmith()


def test_disabled_returns_original_function(tracing):
    tracing(False)

    def work(x):
        return x + 1

    assert ls.traceable("work")(work) is work      # 不是包装器，零开销
    assert ls.tracing_enabled() is False


def test_enabled_wraps_and_preserves_result(tracing):
    tracing(True)

    def work(x):
        """doc"""
        return x + 1

    wrapped = ls.traceable("work")(work)
    assert wrapped is not work                    # 开了才包一层
    assert wrapped.__doc__ == "doc"               # functools.wraps 保住元信息

    tracing(False)                                # 关掉上报，单测不往外发 trace
    assert wrapped(1) == 2


def test_flag_without_key_stays_off(tracing):
    tracing(True, key="")
    assert ls.tracing_enabled() is False
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_configure_writes_and_clears_env(tracing):
    tracing(True)
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_pt_test"
    assert os.environ["LANGSMITH_PROJECT"] == settings.langsmith_project

    tracing(False)
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert "LANGSMITH_API_KEY" not in os.environ    # 关掉时摘掉 key，避免残留上报


def test_public_config_never_leaks_key(tracing, monkeypatch):
    """前端回链只带开关 / 项目名 / 项目地址，key 不能出现在响应里。"""
    url = "https://smith.langchain.com/o/acme/projects/p/123"
    monkeypatch.setattr(settings, "langsmith_project_url", url)
    tracing(True)

    cfg = ls.public_config()
    assert cfg == {"enabled": True, "project": settings.langsmith_project, "url": url}
    assert "lsv2_pt_test" not in str(cfg)          # key 不进 public_config

    tracing(False)
    monkeypatch.setattr(settings, "langsmith_project_url", "")
    off = ls.public_config()
    assert off["enabled"] is False and off["url"] == ""


def test_llm_client_wrapped_only_when_enabled(tracing, monkeypatch):
    import types

    from src.app.llm import provider

    monkeypatch.setattr(provider, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(provider, "LLM_BASE_URL", "")

    tracing(False)
    plain = provider.get_llm()
    # 未包装时 create 是类的绑定方法
    assert isinstance(plain.chat.completions.create, types.MethodType)

    tracing(True)
    traced = provider.get_llm()
    # wrap_openai 在实例上挂了一层普通函数（用来上报 trace），调用方式不变
    assert isinstance(traced.chat.completions.create, types.FunctionType)
    assert hasattr(traced, "chat")
