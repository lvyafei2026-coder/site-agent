# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""提示词集中化守卫：所有发给 LLM 的文本只能在 src/app/prompts/versions/ 下。

回归背景（2026-11-13）：逐词分析的 ``AI_SYSTEM``（1600 余字）与 user 模板原本硬编码在
``tools/adapters/sources/hotwords.py``，两个节点的 human 模板也是代码里的字面量。改提示词
得翻代码、还容易把「唯一真源」弄出两份。这里同时守住三件事：
  1. 目录清单 / 默认版本 / 版本文件三者一一对应；
  2. 业务代码里不再出现整段提示词；
  3. 迁移后 build_messages 的输出与迁移前**逐字一致**（AI 结论口径不能被搬家改掉）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from src.app.config import settings
from src.app.prompts import PROMPT_TASKS, available_versions, load_prompt
from src.app.tools.adapters.sources import hotwords as hw

APP = Path(__file__).resolve().parents[1] / "src" / "app"

# 迁移前实测（2026-11-13，从 hotwords.py 原 AI_SYSTEM / build_messages 取样）
SYSTEM_LEN = 1631
SYSTEM_SHA = "3cba194f780f7f61"          # sha256 前 16 位
USER_SAMPLES = [
    ({"term": "nayyi naveli", "related": ["a", "b", "c", "d", "e", "f", "g"],
      "hints": ["工具查询"], "geoCount": 3, "traffic": 900,
      "serp": [{"domain": "imdb.com", "title": "Nayyi Naveli (2026 film)"},
               {"domain": "en.wikipedia.org", "title": "Nayyi   Naveli"}]},
     "90ad5dd4d5a6330e"),
    ({"term": "x", "related": [], "hints": []}, "0999c4bacd404e50"),
    ({"term": "medicare part b premium 2027 calculator",
      "related": ["2027 calculator", "part b deductible"],
      "hints": ["工具查询", "资格政策"], "geoCount": 12, "traffic": 20000,
      "serp": [{"domain": "a.com", "title": "A"}, {"domain": "b.com", "title": ""}]},
     "c6e6c088e92af27c"),
]

# 只要这些片段出现在任何 .py 里，就说明又有人把提示词粘回代码了
FORBIDDEN_IN_CODE = (
    "你是给独立开发者做 SEO 工具站的顾问",
    "严格按下面的小节输出，全中文",
    "判断要点，按顺序判",
    "关键词：{term}",
    '("human", "请求：{input_text}")',
    "意图：{intent}\\n请求：{input_text}",
)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def test_catalog_matches_directories_and_default_versions():
    """PROMPT_TASKS / config 默认版本 / versions 下的目录，三者必须一一对应。"""
    root = settings.prompt_root
    dirs = {p.name for p in root.iterdir() if p.is_dir() and list(p.glob("v*.txt"))}
    assert dirs == set(PROMPT_TASKS), f"目录与清单不一致：{dirs ^ set(PROMPT_TASKS)}"
    assert set(settings.default_prompt_versions) == dirs
    for task, version in settings.default_prompt_versions.items():
        assert version in available_versions(task), f"{task} 缺默认版本 {version}"
        assert load_prompt(task, version).strip()
    assert available_versions("intent") == ["v1", "v2"]


def test_missing_version_falls_back_to_v1():
    """请求传了不存在的版本要回退 v1，不能让整批分析因为拼错版本号而崩。"""
    assert load_prompt("hotwords", "v999") == load_prompt("hotwords", "v1")
    assert load_prompt("intent", "v999") == load_prompt("intent", "v1")
    assert available_versions("no_such_task") == []


def test_no_prompt_literals_left_in_source():
    """扫全量 .py：整段提示词（含两个节点的 human 字面量）不允许再出现在代码里。"""
    offenders = []
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_IN_CODE:
            if needle in text:
                offenders.append(f"{path.relative_to(APP)}: {needle[:24]}")
    assert not offenders, "提示词又粘回代码了：" + "; ".join(offenders)


def test_hotword_system_is_byte_identical_after_move():
    """★逐字一致：system 正文一个字都不能变，否则 AI 的 8 小节/判死口径全变了。"""
    system = load_prompt("hotwords")
    assert len(system) == SYSTEM_LEN
    assert _sha(system) == SYSTEM_SHA
    assert "8) 信号标签" in system
    assert "⓪ 先辨形" in system and "③ 其余情况" in system
    assert hw._prompt_pair()[0] == system


def test_hotword_user_template_placeholders_and_output():
    """user 模板只用命名占位符；三组样例（有/无 SERP、有/无信号）输出逐字一致。"""
    tpl = load_prompt("hotwords_user")
    assert set(re.findall(r"\{(\w+)\}", tpl)) == {
        "term", "related_line", "serp_line", "hints_line", "geo_count", "traffic",
    }
    for body, expected in USER_SAMPLES:
        msgs = hw.build_messages(body)
        assert msgs[0]["role"] == "system" and msgs[0]["content"] == load_prompt("hotwords")
        assert _sha(msgs[1]["content"]) == expected
        assert "{term}" not in msgs[1]["content"]


def test_prompt_pair_is_cached(monkeypatch):
    """一批 20 词只读一次磁盘：system + user 各一次，之后全走进程内缓存。"""
    hw._prompt_pair.cache_clear()
    calls = {"n": 0}
    real = hw.load_prompt

    def counting(task, version=None):
        calls["n"] += 1
        return real(task, version)

    monkeypatch.setattr(hw, "load_prompt", counting)
    hw._prompt_pair()
    hw._prompt_pair()
    assert calls["n"] == 2
    assert real("hotwords", "v999") == real("hotwords", "v1")  # 版本不存在回退 v1
    hw._prompt_pair.cache_clear()


def test_prompt_version_reaches_analyze_hotwords_tool():
    """/run 的 prompt_versions.hotwords 要一路透传到工具入参，否则版本化形同虚设。"""
    from src.app.services.tool_service import ToolService

    captured: dict = {}

    class FakeRegistry:
        def names(self):
            return ["analyze_hotwords"]

        def run(self, name, **kwargs):
            captured.update(kwargs)
            return {"tool": name, "ok": True, "result": {}}

    ToolService(FakeRegistry()).execute_for_intent(
        "analyze_hotwords", "分析热词", {}, prompt_versions={"hotwords": "v2"})
    assert captured.get("limit") == 20
    assert captured.get("prompt_version") == "v2"

    captured.clear()
    ToolService(FakeRegistry()).execute_for_intent("analyze_hotwords", "分析热词", {})
    assert "prompt_version" not in captured     # 没指定版本就不该塞参数
