# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""配置、状态契约、checkpointer。"""

import json

import pytest

from src.app.config import (
    FLEET_DOMAIN, INTENTS, SITES, Settings, load_sites, settings,
)
from src.app.memory.checkpointer import get_checkpointer


def test_settings_contract():
    assert isinstance(settings, Settings)
    assert settings.project_name == "site-agent"
    # 提示词真源在 D1，config 只保留各 task 的兜底默认版本号
    from src.app.prompts import PROMPT_TASKS
    assert set(settings.default_prompt_versions) == set(PROMPT_TASKS)


def test_intents_are_the_single_source():
    assert len(INTENTS) == len(set(INTENTS)) == 6
    assert "general" in INTENTS
    assert "analyze_hotwords" in INTENTS


def test_fleet_domain_is_single_source():
    assert FLEET_DOMAIN and SITES
    assert SITES["music"].host.endswith(FLEET_DOMAIN)
    assert SITES["music"].url.startswith("https://")


def test_sites_come_from_json_config():
    """站点清单在 config/sites.json，不在 py 里硬编码。"""
    assert SITES["crcl"].repo_name == "cockcroft-gault-calculator"
    assert SITES["crcl"].host == f"crcl.{FLEET_DOMAIN}"
    assert SITES["crcl"].asset_dir == "public"
    assert SITES["devtools"].lang == "zh"
    for site in SITES.values():
        assert site.repo_name and site.subdomain and site.asset_dir


def test_load_sites_defaults_and_duplicate_guard(tmp_path):
    path = tmp_path / "sites.json"
    path.write_text(json.dumps({"sites": [
        {"name": "plain"},
        {"name": "custom", "repo": "custom-repo", "lang": "zh",
         "asset_dir": "web", "daily_limit": 2},
    ]}), encoding="utf-8")
    sites = load_sites(path)
    assert sites["plain"].repo_name == "plain"
    assert sites["plain"].subdomain == "plain"
    assert (sites["plain"].asset_dir, sites["plain"].branch) == ("public", "main")
    assert sites["custom"].repo_name == "custom-repo"
    assert (sites["custom"].asset_dir, sites["custom"].daily_limit) == ("web", 2)

    path.write_text(json.dumps({"sites": [{"name": "dup"}, {"name": "dup"}]}),
                    encoding="utf-8")
    with pytest.raises(ValueError):
        load_sites(path)


def test_checkpointer_memory(monkeypatch):
    monkeypatch.setattr(settings, "memory_backend", "memory")
    saver = get_checkpointer()
    assert type(saver).__name__ in ("MemorySaver", "InMemorySaver")


def test_checkpointer_sqlite_is_a_saver_not_a_context_manager(tmp_path, monkeypatch):
    # from_conn_string 返回上下文管理器，直接交给 compile 会在首次写入时炸
    monkeypatch.setattr(settings, "memory_backend", "sqlite")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'agent.db'}")
    saver = get_checkpointer()
    assert hasattr(saver, "get_tuple")


def test_checkpointer_rejects_unknown_backend(monkeypatch):
    monkeypatch.setattr(settings, "memory_backend", "redis")
    with pytest.raises(ValueError):
        get_checkpointer()


def test_workflow_state_is_typeddict():
    from src.app.graph.state import WorkflowState
    assert "tool_results" in WorkflowState.__annotations__
    assert "requested_tools" in WorkflowState.__annotations__


def test_initial_state_defaults():
    from src.app.graph.state import initial_workflow_state
    state = initial_workflow_state("hi", intent="general", tools=["get_flag"])
    assert state["retry_count"] == 0
    assert state["messages"] == []
    assert state["tool_results"] == {}
    assert state["requested_tools"] == ["get_flag"]
    # ★不能预填 config 的兜底版本表（否则 D1 里切的默认版本永远读不到）
    assert state["prompt_versions"] == {}
