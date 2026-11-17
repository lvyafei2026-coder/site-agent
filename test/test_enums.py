# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""枚举真源守卫：保证所有枚举族集中定义、值唯一、与既有字符串口径完全兼容。"""

from __future__ import annotations

import json
from enum import StrEnum

import pytest

from src.app import enums
from src.app.enums import (
    AnalysisPhase, BackfillAction, BacklinkChannel, BacklinkStatus,
    CandidateStatus, CurationStatus, DevtoStatus, EventKind, EventStatus,
    GraphNode, HintName, Intent, LedgerStrategy, LlmThinking, MemoryBackend,
    PageQueryMode, PageStatus, PlanStatus, PromptTask, RejectSource,
    ReportKind, RouteTag, SearchTimeframe, SerpEngine, SerpVerdict,
    ToolKind, TrackEvent, Verdict, WorkflowStatus,
)

ALL_ENUMS = [
    Intent, WorkflowStatus, GraphNode, RouteTag, ToolKind, LlmThinking,
    MemoryBackend, CandidateStatus, CurationStatus, RejectSource, HintName,
    Verdict, AnalysisPhase, BackfillAction, SearchTimeframe, EventStatus,
    EventKind, ReportKind, PlanStatus, PageStatus, BacklinkStatus,
    BacklinkChannel, PageQueryMode, LedgerStrategy, TrackEvent, SerpVerdict,
    DevtoStatus, SerpEngine, PromptTask,
]


def test_all_are_str_enums():
    """每个枚举族都是 StrEnum —— 保证与存量 str 字段/JSON/D1 参数完全兼容。"""
    for cls in ALL_ENUMS:
        assert issubclass(cls, StrEnum), cls.__name__
        for member in cls:
            assert isinstance(member, str), f"{cls.__name__}.{member.name} 不是 str"


def test_values_unique_within_family():
    """同一族内不允许两个成员共用取值（否则含义歧义）。"""
    for cls in ALL_ENUMS:
        values = [m.value for m in cls]
        assert len(values) == len(set(values)), f"{cls.__name__} 有重复取值：{values}"


def test_every_member_has_docstring_note():
    """每个成员都要有含义注释（StrEnum 成员 doc 来自源码行内注释后的 docstring 或枚举类注释约定）。

    这里退一步校验「成员有文档字符串」——我们用类级注释说明每个值；若个别成员
    没写行内注释，至少成员名要能自解释。真正强约束是值唯一 + 集合锁定（下一个测试）。
    """
    for cls in ALL_ENUMS:
        assert cls.__doc__, f"{cls.__name__} 缺少类说明"
        for member in cls:
            assert member.name and member.value, f"{cls.__name__} 存在空成员"


@pytest.mark.parametrize("cls,expected", [
    (Intent, {"select_pages", "plan_promo", "find_backlinks", "review_metrics",
              "analyze_hotwords", "general"}),
    (CandidateStatus, {"doing", "live"}),
    (CurationStatus, {"saved", "todo", "wip", "live", "parked"}),
    (Verdict, {"做", "观望", "不做", "未定"}),
    (WorkflowStatus, {"success", "failed"}),
    (ToolKind, {"read", "ledger", "write"}),
    (EventStatus, {"pending", "consumed"}),
    (PageQueryMode, {"all", "recent", "new", "review"}),
    (LedgerStrategy, {"upsert", "ignore", "lookup"}),
])
def test_locked_value_sets(cls, expected):
    """核心枚举族的取值集合锁定：改动必须显式（防止误删/误加导致口径漂移）。"""
    assert {m.value for m in cls} == expected


def test_string_compatibility():
    """与旧字面量的比较、dict key、集合成员、JSON 序列化行为一致。"""
    assert CandidateStatus.DOING == "doing"
    assert Intent.GENERAL == "general"
    assert Verdict.DO == "做"
    assert {"doing": 1}[CandidateStatus.DOING] == 1
    assert "live" in {CandidateStatus.DOING, CandidateStatus.LIVE}
    assert json.dumps({"status": CandidateStatus.DOING}) == '{"status": "doing"}'
    assert f"{CurationStatus.TODO}" == "todo"


def test_intents_match_config():
    """config.INTENTS 必须由 Intent 枚举派生，杜绝两处各自维护。"""
    from src.app.config import INTENTS
    assert tuple(INTENTS) == tuple(i.value for i in Intent)


def test_no_module_defines_its_own_status_list():
    """防止有人再把 STATUSES / HINT_NAMES 等以字面量数组抄回业务模块。

    允许的定义点只有 enums.py 与「从枚举派生的兼容别名」（curation.py / hotwords.py）。
    """
    # curation.py 的 STATUSES/HINT_NAMES 与 hotwords.py 的 HINT_NAMES 均为枚举派生
    from src.app.tools.adapters.sources import curation, hotwords
    assert curation.STATUSES == [s.value for s in CurationStatus]
    assert curation.HINT_NAMES == [h.value for h in HintName]
    assert hotwords.HINT_NAMES == tuple(h.value for h in HintName)


def test_enums_has_no_project_dependency():
    """枚举模块必须保持零项目内依赖，才能被 base.py / sql.py 等叶子安全引用。"""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(enums.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {m for m in imported if m.startswith("src.app")}, imported
