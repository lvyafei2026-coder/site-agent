# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""提示词集中化守卫（D1 真源版）：所有发给 LLM 的文本只能来自 radar-rules D1。

回归背景（2026-11-13 → 11-17）：逐词分析的 ``AI_SYSTEM``（1600 余字）与 user 模板
原本硬编码在 ``tools/adapters/terms/hotwords.py``（其判据层现为 hotword_criteria.py），先收拢到本地 versions/*.txt，
再整体迁进 D1（prompts / prompt_defaults 两表），本地 txt 已删除。这里同时守住：
  1. 业务代码里不再出现整段提示词（反粘回）；
  2. task 清单 / config 默认版本 / D1 桩三者对应；
  3. 拼装逻辑（build_messages 占位符、判例注入、版本透传）不因搬家而退化。

★D1 化后文案由 /rules/prompts 页面维护、随时可变，因此不再锁真实文案逐字 SHA；
  测试用 conftest 的桩验证结构契约（8 小节 / 结论格式 / 占位符）。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.app.config import settings
from src.app.prompts import PROMPT_TASKS, available_versions, load_prompt
from src.app.tools.adapters.terms import hotwords as hw
from src.app.tools.adapters.terms import hotword_criteria as hwc  # _prompt_pair 真身所在

APP = Path(__file__).resolve().parents[1] / "src" / "app"

# 只要这些片段出现在任何 src/app/*.py 里，就说明又有人把提示词粘回代码了。
FORBIDDEN_IN_CODE = (
    "你是给独立开发者做 SEO 工具站的顾问",
    "严格按下面的小节输出，全中文",
    "判断要点，按顺序判",
    "关键词：{term}",
    '("human", "请求：{input_text}")',
    "意图：{intent}\\n请求：{input_text}",
)


def test_catalog_matches_default_versions():
    """PROMPT_TASKS / config 默认版本 / D1 桩三者一一对应，每个默认版本都可加载。"""
    assert set(settings.default_prompt_versions) == set(PROMPT_TASKS)
    for task, version in settings.default_prompt_versions.items():
        assert version in available_versions(task), f"{task} 缺默认版本 {version}"
        assert load_prompt(task, version).strip()
    assert available_versions("intent") == ["v1", "v2"]


def test_missing_version_falls_back_to_default():
    """请求不存在的版本回退到该 task 默认版本，不让整批分析因版本号写错而崩。"""
    assert load_prompt("hotwords", "v999") == load_prompt("hotwords")   # hotwords 默认 v2
    assert load_prompt("intent", "v999") == load_prompt("intent", "v1")
    assert available_versions("no_such_task") == []


def test_no_prompt_literals_left_in_source():
    """扫全量 src/app/*.py：整段提示词（含两个节点的 human 字面量）不允许再出现在代码里。"""
    offenders = []
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_IN_CODE:
            if needle in text:
                offenders.append(f"{path.relative_to(APP)}: {needle[:24]}")
    assert not offenders, "提示词又粘回代码了：" + "; ".join(offenders)


def test_hotword_system_structure_contract():
    """system 的 8 小节与结论格式是 verdict_of / signals_of 的解析契约，不能丢。"""
    system = load_prompt("hotwords", "v1")
    assert "8) 信号标签" in system
    assert "⓪ 先辨形" in system and "③ 其余情况" in system
    v2 = load_prompt("hotwords", "v2")
    assert "7) 结论：做 / 观望 / 不做" in v2
    assert load_prompt("hotwords") == v2          # 默认就是 v2
    assert hw._prompt_pair()[0] == v2


def test_hotword_user_template_placeholders():
    """user 模板只用命名占位符；v2 多 {examples_line}，v1 没有。"""
    assert set(re.findall(r"\{(\w+)\}", load_prompt("hotwords_user", "v2"))) == {
        "term", "related_line", "serp_line", "hints_line", "geo_count", "traffic",
        "examples_line",
    }
    assert "{examples_line}" not in load_prompt("hotwords_user", "v1")


def test_examples_injected_only_when_present():
    """判例只在带 {examples_line} 的模板里注入，且格式含「过去判定不做」提示。"""
    ex = [{"term": "how to sound better", "reason": "改写润色，一次性答案"}]
    msgs = hw.build_messages({"term": "make this text better", "related": [], "hints": []},
                             examples=ex)
    assert "过去判定「不做」" in msgs[1]["content"]
    assert "how to sound better — 改写润色，一次性答案" in msgs[1]["content"]

    # v1 模板无 {examples_line} 占位符：即使传 examples 也不注入、不报 KeyError
    msgs_v1 = hw.build_messages({"term": "x", "related": [], "hints": []},
                                prompt_version="v1", examples=ex)
    assert "{examples_line}" not in msgs_v1[1]["content"]
    assert "人工判定" not in msgs_v1[1]["content"]
    assert "how to sound better" not in msgs_v1[1]["content"]


def test_build_messages_renders_all_fields():
    """占位符全部被真实数据替换：不留裸 {}、关联词截断 6 条、SERP 标题进 user。"""
    msgs = hw.build_messages({
        "term": "demo term", "related": ["a", "b", "c", "d", "e", "f", "g"],
        "hints": ["工具查询"], "geoCount": 3, "traffic": 900,
        "serp": [{"domain": "imdb.com", "title": "Demo (2026 film)"}],
    })
    body = msgs[1]["content"]
    assert msgs[0]["content"] == load_prompt("hotwords")
    assert "关键词：demo term" in body
    assert "关联查询：a / b / c / d / e / f\n" in body and " / g" not in body
    assert "Demo (2026 film)" in body
    assert "命中的常青信号：工具查询" in body
    assert "覆盖地区数：3 个" in body
    assert not re.search(r"\{(term|related_line|serp_line|hints_line|geo_count|traffic)\}", body)


def test_prompt_pair_is_cached():
    """一批 20 词只读一次 D1 快照：system + user 各一次，之后全走缓存。"""
    hw._prompt_pair.cache_clear()
    calls = {"n": 0}
    real = hwc.load_prompt

    def counting(task, version=None):
        calls["n"] += 1
        return real(task, version)

    from unittest.mock import patch
    with patch.object(hwc, "load_prompt", counting):
        hw._prompt_pair()
        hw._prompt_pair()
    assert calls["n"] == 2
    hw._prompt_pair.cache_clear()


