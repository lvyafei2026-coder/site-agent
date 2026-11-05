# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE。

"""纯函数校验测试（不依赖网络与环境变量）。"""

from site_agent.cf.deploy import SLUG_RE
from site_agent.tasks.gen_page import is_dead
from site_agent.tasks.promo import validate


def test_x_draft_with_url_is_rejected():
    problems = validate(
        {
            "channel": "x",
            "title": "launch",
            "draft": "Try our new tool today, it is fast and free https://music.toolara.dev",
        }
    )
    assert any("外链" in p for p in problems)


def test_x_draft_without_url_passes():
    problems = validate(
        {
            "channel": "x",
            "title": "launch",
            "draft": "Turn a text prompt into a full song with your own lyrics. Demo in the thread.",
        }
    )
    assert problems == []


def test_reddit_new_account_blocked():
    problems = validate(
        {
            "channel": "reddit",
            "title": "reply",
            "draft": "You can just paste the lyrics and it renders the whole track for you.",
        }
    )
    assert any("Reddit" in p for p in problems)


def test_short_draft_rejected():
    problems = validate({"channel": "devto", "title": "t", "draft": "too short"})
    assert any("过短" in p for p in problems)


def test_unknown_channel_rejected():
    problems = validate({"channel": "tiktok", "title": "t", "draft": "a" * 60})
    assert any("未知渠道" in p for p in problems)


def test_dead_rules_block_person_names():
    assert is_dead("Taylor Swift net worth")
    assert is_dead("who is the ceo of openai")
    assert not is_dead("free video compressor online")


def test_slug_pattern():
    assert SLUG_RE.match("free-video-compressor")
    assert not SLUG_RE.match("Free Video Compressor")
    assert not SLUG_RE.match("../etc/passwd")
    assert not SLUG_RE.match("ab")
