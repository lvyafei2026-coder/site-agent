# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""分层守卫：依赖只能单向。违反时这里先红，而不是部署后 ModuleNotFoundError。"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src" / "app"

# src.app.enums 是零依赖的纯数据模块（只 import 标准库 enum），不含任何 IO / 服务
# 装配 / 循环引用风险。2026-11-17 起允许叶子模块 base.py、sql.py 引用它取枚举值，
# 这不破坏「叶子」的实质（无副作用、无反向依赖）。
ENUMS = "src.app.enums"


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


def test_services_never_depend_on_nodes_or_graph():
    """services 是节点的实现类，只能被 nodes 调，不能反过来认识 nodes / graph。

    节点 ↔ 实现类的对应表在 services/__init__.py；一旦 service 里出现图或节点，
    说明业务逻辑又漏回编排层了。
    """
    offenders = {m for m in imports_of("services")
                 if m.startswith(("src.app.nodes", "src.app.graph"))}
    assert not offenders, f"services 反向依赖编排层：{sorted(offenders)}"


def test_nodes_dispatch_by_name_never_by_tool_literal():
    """编排层只说名字，不说工具名：意图 → INTENT_TOOLS、逐词步骤 → STEP_TOOLS。

    两张路由表都在 services/tool_service.py，节点里再写一遍 registry 的工具名就是把
    「哪一步调哪个工具」拆成两处真源——改工具名时节点会静默调到不存在的名字。
    同时锁住覆盖关系：图上能连的步骤，路由表必须有对应行。
    """
    from src.app.graph.hotword_graph import EVIDENCE_STEPS, POST_STEPS
    from src.app.services.tool_service import INTENT_TOOLS, STEP_TOOLS

    routed = {n for names in INTENT_TOOLS.values() for n in names} | set(STEP_TOOLS.values())
    offenders = set()
    for path in (APP / "nodes").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in routed:
                    offenders.add(f"{path.name}::{node.value}")
    assert not offenders, f"节点里写死了 registry 工具名：{sorted(offenders)}"

    # 图连线名单 ⊆ 工具路由表；fetch / examples 每轮必做，不在可选名单里也有路由
    assert set(EVIDENCE_STEPS) | set(POST_STEPS) <= set(STEP_TOOLS)


def test_adapters_are_leaves():
    """adapters 是最外层实现，不能反过来依赖 tools 契约、注册表、services 或编排层。"""
    offenders = {m for m in imports_of("tools/adapters")
                 if m.startswith(("src.app.tools.base", "src.app.tools.registry",
                                  "src.app.services", "src.app.graph", "src.app.nodes"))}
    assert not offenders, f"adapters 反向依赖：{sorted(offenders)}"


def test_entry_depends_only_on_graph():
    """入口层（main / api）只依赖 graph + 横切基础设施，不穿透 services / tools / adapters。

    取图、跑热词图、运维回填都从 graph 包走（graph/__init__.py 统一门面）。
    config / enums / observability / llm / utils / models 是横切，各层皆可读。
    api 属于入口层自身：main.py 装配 uvicorn app 时必须 import api.routes，
    这不算穿透下层（api/routes.py 本身也受同一守卫约束）。
    """
    allowed = ("src.app.graph", "src.app.api", "src.app.config", "src.app.enums",
               "src.app.observability", "src.app.llm", "src.app.utils", "src.app.models")
    for rel in ("main.py", "api/routes.py"):
        tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        offenders = {m for m in imported
                     if m.startswith("src.app.") and not m.startswith(allowed)}
        assert not offenders, f"{rel} 穿透分层：{sorted(offenders)}"


def test_base_is_a_pure_leaf():
    """base.py 只依赖标准库与 pydantic：门面靠注入，不 import 任何包内模块。"""
    tree = ast.parse((APP / "tools" / "base.py").read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module)
    offenders = {m for m in top_level if m.startswith("src.app") and m != ENUMS}
    assert not offenders, f"base.py 顶层 import 了包内模块：{sorted(offenders)}"


def test_tools_layer_does_no_direct_io():
    """外部 IO 只在 adapters 里发生：工具层不得自己发请求 / 起子进程。

    base.py 例外——它只 import requests / socket 的**异常类型**做重试分类，不发请求。
    """
    banned = {"requests", "subprocess", "socket", "httpx", "urllib"}
    offenders = set()
    for path in (APP / "tools").glob("*.py"):
        if path.name == "base.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module
                     else [])
            for name in names:
                if name.split(".")[0] in banned:
                    offenders.add(f"{path.name}::{name}")
    assert not offenders, f"工具层自己做了 IO：{sorted(offenders)}"


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


def test_entry_and_graph_have_no_deferred_imports():
    """入口层与图装配不得再用函数内 import 解环——环已从结构上消除。

    main.py 顶层 import 全部依赖；热词图的装配与运行入口同处 hotword_graph.py。
    这些文件若重现函数内 import，说明有人把加载期环又引回来了。
    """
    for rel in ("main.py", "graph/hotword_graph.py", "graph/normal_graph.py"):
        tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
        offenders = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        offenders.add(node.name)
        assert not offenders, f"{rel} 出现函数内 import：{sorted(offenders)}"


def test_no_deferred_internal_imports():
    """全仓禁止函数内 import src.app.*：包内模块的依赖必须在顶层声明。

    第三方 SDK 的按需加载（langsmith / openai / google 等）不在此列——那是关闭追踪
    或无凭据时的零开销设计，不是拿延迟导入解加载期环。
    """
    offenders = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and (sub.module or "").startswith("src.app"):
                    offenders.add(f"{path.relative_to(APP)}::{node.name}")
                elif isinstance(sub, ast.Import):
                    for alias in sub.names:
                        if alias.name.startswith("src.app"):
                            offenders.add(f"{path.relative_to(APP)}::{node.name}")
    assert not offenders, f"函数内延迟 import 包内模块：{sorted(offenders)}"


def test_business_functions_have_no_test_injection_params():
    """生产函数不得为测试留注入口：这些参数名 + 默认值 None 已从签名里清掉。

    需要换实现时由测试侧 monkeypatch 取数点（get_kv / requests.Session / store._fetcher），
    不许再给业务函数加「平时永远不传」的可选参数。
    """
    banned = {"kv", "session", "transport", "fetcher", "now", "backend", "database_url"}
    offenders = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            pairs = list(zip(args.args[-len(args.defaults):], args.defaults)) if args.defaults else []
            pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
            for arg, default in pairs:
                if (arg.arg in banned and isinstance(default, ast.Constant)
                        and default.value is None):
                    offenders.append(f"{path.relative_to(APP)}::{node.name}({arg.arg}=None)")
    assert not offenders, f"业务函数又出现测试注入参数：{sorted(offenders)}"


def test_sql_layer_has_no_side_effects():
    """sql.py 只放语句与构造器，不碰 client / requests / env。"""
    module = ast.parse((APP / "tools/adapters/cloudflare/sql.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported <= {"__future__", "dataclasses", "typing", ENUMS}, imported
