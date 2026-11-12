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
    assert settings.prompt_root.name == "versions"
    assert set(settings.default_prompt_versions) == {"intent", "extraction"}


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


def test_checkpointer_memory():
    saver = get_checkpointer(backend="memory")
    assert type(saver).__name__ in ("MemorySaver", "InMemorySaver")


def test_checkpointer_sqlite_is_a_saver_not_a_context_manager(tmp_path):
    # from_conn_string 返回上下文管理器，直接交给 compile 会在首次写入时炸
    saver = get_checkpointer(backend="sqlite",
                             database_url=f"sqlite:///{tmp_path / 'agent.db'}")
    assert hasattr(saver, "get_tuple")


def test_checkpointer_rejects_unknown_backend():
    with pytest.raises(ValueError):
        get_checkpointer(backend="redis")


def test_workflow_state_is_typeddict():
    from src.app.graph.state import WorkflowState
    assert "tool_results" in WorkflowState.__annotations__
    assert "requested_tools" in WorkflowState.__annotations__


def test_initial_state_defaults():
    from src.app.graph.state import initial_state
    state = initial_state("hi", intent="general", tools=["get_flag"])
    assert state["retry_count"] == 0
    assert state["messages"] == []
    assert state["tool_results"] == {}
    assert state["requested_tools"] == ["get_flag"]
    assert state["prompt_versions"] == settings.default_prompt_versions
