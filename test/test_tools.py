# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""26 个 BaseTool 子类：注册、Pydantic 契约、执行结果形状。"""

from pydantic import BaseModel

from src.app.tools.base import BaseTool, RuntimeTool, ToolOutput, is_retryable_error
from src.app.tools.registry import tool_registry


def test_registry_has_26_tools():
    assert len(tool_registry.names()) == 26
    assert len(set(tool_registry.names())) == 26


def test_kind_split():
    assert len(tool_registry.by_kind("read")) == 13
    assert len(tool_registry.by_kind("ledger")) == 10
    assert len(tool_registry.by_kind("write")) == 3


def test_every_tool_is_basetool_with_pydantic_input():
    for name in tool_registry.names():
        tool = tool_registry.get(name)
        assert isinstance(tool, BaseTool), name
        assert issubclass(tool.Input, BaseModel), name
        assert tool.name == name
        # description 取自类 docstring：写漏 docstring 等于模型看不到工具用途
        assert tool.description == (type(tool).__doc__ or "").strip(), name
        assert tool.description, name


def test_run_returns_unified_output(fake):
    out = tool_registry.run("get_hotwords", limit=5)
    assert ToolOutput(**out).ok is True
    assert out["tool"] == "get_hotwords"
    assert len(out["result"]["items"]) == 2


def test_missing_required_arg_is_caught(fake):
    out = tool_registry.run("get_flag")  # key 必填
    assert out["ok"] is False
    assert out["error"]


def test_unknown_tool_is_reported():
    from src.app.services.tool_service import tool_service
    out = tool_service.execute_for_intent("general", "x", {}, tools=["not_a_tool"])
    assert out["not_a_tool"]["ok"] is False


def test_every_tool_method_is_implemented_and_used():
    """method 名字写错只会在跑图时报错，所以在这里锁住：全部有实现、且没有空转实现。"""
    from src.app.tools.registry import RuntimeService

    rt = RuntimeService()
    declared = {t.method for t in (tool_registry.get(n) for n in tool_registry.names())
                if t.method}
    implemented = {m for m in dir(rt) if not m.startswith("_")}
    assert declared == implemented



def test_write_promo_slot_dedup(fake):
    item = {"date": "2026-11-10", "channel": "x", "title": "launch"}
    first = tool_registry.run("write_promo_slot", item=item)
    second = tool_registry.run("write_promo_slot", item=item)
    assert first["result"]["status"] == "scheduled"
    assert second["result"]["status"] == "skipped"


def test_is_retryable_error_classifies():
    import requests

    # 网络 / 连接层瞬态故障 → 重试
    assert is_retryable_error(requests.exceptions.ConnectTimeout("x")) is True
    assert is_retryable_error(requests.exceptions.ReadTimeout("x")) is True
    assert is_retryable_error(requests.exceptions.ConnectionError("x")) is True
    assert is_retryable_error(TimeoutError("x")) is True
    assert is_retryable_error(ConnectionError("x")) is True

    # 明确的非网络失败 → 不重试
    assert is_retryable_error(ValueError("bad arg")) is False
    assert is_retryable_error(KeyError("k")) is False
    assert is_retryable_error(RuntimeError("boom")) is False


def test_runtime_tool_run_marks_retryable_on_network_failure():
    import requests

    class _Empty(BaseModel):
        pass

    class _NetOps:
        def boom(self):
            raise requests.exceptions.ConnectTimeout("timed out")

    class _NetTool(RuntimeTool):
        tool_name = "net_boom"
        method = "boom"
        Input = _Empty

    out = _NetTool(_NetOps()).run()
    assert out["ok"] is False
    assert out["retryable"] is True


def test_runtime_tool_run_marks_non_retryable_on_clear_failure():
    class _Empty(BaseModel):
        pass

    class _ClearOps:
        def boom(self):
            raise ValueError("bad config")

    class _ClearTool(RuntimeTool):
        tool_name = "clear_boom"
        method = "boom"
        Input = _Empty

    out = _ClearTool(_ClearOps()).run()
    assert out["ok"] is False
    assert out["retryable"] is False


def test_retryable_field_defaults_false_on_success(fake):
    out = tool_registry.run("get_hotwords", limit=5)
    assert out["retryable"] is False
