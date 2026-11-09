# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""分层守卫：依赖只能单向。违反时这里先红，而不是部署后 ModuleNotFoundError。"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src" / "app"


def imports_of(package: str) -> set[str]:
    """递归收集某个包内所有 import 的模块名（含函数内局部导入）。"""
    found: set[str] = set()
    for path in (APP / package).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
    return found


def test_tools_never_depend_on_services():
    """门面在 tools/base.py：tools 一旦反向依赖 services 就是包级循环。"""
    offenders = {m for m in imports_of("tools") if m.startswith("src.app.services")}
    assert not offenders, f"tools 反向依赖 services：{sorted(offenders)}"


def test_adapters_are_leaves():
    """adapters 是最外层实现，不能反过来依赖 tools 的契约或注册表。"""
    offenders = {m for m in imports_of("tools/adapters")
                 if m.startswith(("src.app.tools.base", "src.app.tools.registry",
                                  "src.app.services"))}
    assert not offenders, f"adapters 反向依赖：{sorted(offenders)}"


def test_base_is_a_pure_leaf():
    """base.py 只依赖标准库与 pydantic：门面靠注入，不 import 任何包内模块。"""
    tree = ast.parse((APP / "tools" / "base.py").read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module)
    offenders = {m for m in top_level if m.startswith("src.app")}
    assert not offenders, f"base.py 顶层 import 了包内模块：{sorted(offenders)}"


def test_tools_have_no_deferred_imports():
    """工具层不用函数内 import 解循环：顺序由模块层级保证 registry → *_tools → base。

    adapters/ 除外：那里允许为可选依赖（google-api-python-client 等）做延迟导入。
    """
    offenders = set()
    for path in (APP / "tools").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    offenders.add(f"{path.name}::{node.name}")
    assert not offenders, f"工具层出现函数内 import：{sorted(offenders)}"


def test_sql_layer_has_no_side_effects():
    """sql.py 只放语句与构造器，不碰 client / requests / env。"""
    module = ast.parse((APP / "tools/adapters/cloudflare/sql.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported <= {"__future__", "dataclasses", "typing"}, imported
