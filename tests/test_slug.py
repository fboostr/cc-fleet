"""SLUG / STATUS / QUESTIONS 协议解析 + 冲突处理。"""

from __future__ import annotations

import pytest

from cc_fleet.core.slug import (
    is_valid_slug,
    parse_plan_output,
    resolve_slug_conflict,
    strip_plan_protocol_tail,
)


def test_parse_ready():
    text = """
    分析完毕。下面是 plan...

    SLUG: add-readme-line
    STATUS: READY
    """
    p = parse_plan_output(text)
    assert p.slug == "add-readme-line"
    assert p.status == "READY"
    assert p.questions == []


def test_parse_need_clarification():
    text = """
    SLUG: add-login
    STATUS: NEED_CLARIFICATION
    QUESTIONS:
    1. 登录使用密码还是 OAuth？
    2. 是否需要"记住我"功能？
    3. 失败次数限制是多少？
    """
    p = parse_plan_output(text)
    assert p.slug == "add-login"
    assert p.status == "NEED_CLARIFICATION"
    assert len(p.questions) == 3
    assert "OAuth" in p.questions[0]
    # 前缀编号应被剥除
    assert not p.questions[0].startswith(("1", "."))


def test_parse_questions_legacy_dash_bullet():
    """向后兼容：历史 plan.md / 复活的旧 session 可能仍用 `- ` 无序 bullet。"""
    text = """
    SLUG: legacy-style
    STATUS: NEED_CLARIFICATION
    QUESTIONS:
    - 一
    - 二
    * 三
    • 四
    """
    p = parse_plan_output(text)
    assert p.questions == ["一", "二", "三", "四"]


def test_parse_questions_numbered_variants():
    """有序编号容忍 `1.` / `2)` / `3、` 三种常见变体。"""
    text = """
    SLUG: numbered-variants
    STATUS: NEED_CLARIFICATION
    QUESTIONS:
    1. 一
    2) 二
    3、 三
    """
    p = parse_plan_output(text)
    assert p.questions == ["一", "二", "三"]


def test_parse_takes_last_occurrence():
    """文档前文若举了示例，正文末尾的真实命中应被采纳。"""
    text = """
    示例：SLUG: example-slug

    （此处不应被采纳）
    STATUS: NEED_CLARIFICATION

    实际输出：
    SLUG: real-slug
    STATUS: READY
    """
    p = parse_plan_output(text)
    assert p.slug == "real-slug"
    assert p.status == "READY"


def test_parse_questions_stops_at_blank_line():
    text = """
    SLUG: x-y-z
    STATUS: NEED_CLARIFICATION
    QUESTIONS:
    1. 一
    2. 二

    其它说明...
    """
    p = parse_plan_output(text)
    assert p.questions == ["一", "二"]


def test_parse_missing_protocol():
    p = parse_plan_output("纯文本，没有任何协议字段")
    assert p.slug is None and p.status is None


# ---- strip_plan_protocol_tail ----


def test_strip_tail_ready():
    text = (
        "# 实施 plan\n\n"
        "1. 改 foo\n"
        "2. 改 bar\n\n"
        "SLUG: add-readme-line\n"
        "STATUS: READY\n"
    )
    body = strip_plan_protocol_tail(text)
    assert body == "# 实施 plan\n\n1. 改 foo\n2. 改 bar"


def test_strip_tail_need_clarification_with_questions():
    text = (
        "## 候选方案\n\n"
        "- 方案 A\n"
        "- 方案 B\n\n"
        "SLUG: add-login\n"
        "STATUS: NEED_CLARIFICATION\n"
        "QUESTIONS:\n"
        "- 密码还是 OAuth？\n"
        "- 是否需要记住我？\n"
    )
    body = strip_plan_protocol_tail(text)
    assert body == "## 候选方案\n\n- 方案 A\n- 方案 B"
    assert "SLUG:" not in body
    assert "QUESTIONS" not in body


def test_strip_tail_handles_embedded_example():
    """plan 正文中前文复述了协议示例，应只剥真正末尾那段。"""
    text = (
        "示例：```\nSLUG: example-foo\nSTATUS: READY\n```\n\n"
        "真正的协议尾在这里：\n\n"
        "SLUG: real-final\n"
        "STATUS: READY\n"
    )
    body = strip_plan_protocol_tail(text)
    # 示例段保留，末尾真协议被剥
    assert "example-foo" in body
    assert "real-final" not in body
    assert "STATUS: READY" in body  # 示例块里的还在


def test_strip_tail_no_protocol_returns_rstripped_original():
    text = "纯文本，没有任何协议字段\n\n   \n"
    assert strip_plan_protocol_tail(text) == "纯文本，没有任何协议字段"


def test_strip_tail_empty_body_when_only_protocol():
    text = "SLUG: foo\nSTATUS: READY\n"
    assert strip_plan_protocol_tail(text) == ""


@pytest.mark.parametrize("good", ["abc", "add-login", "a1-b2-c3", "x" * 80])
def test_is_valid_slug_good(good: str):
    assert is_valid_slug(good)


@pytest.mark.parametrize("bad", ["", "ab", "-leading", "UPPER", "中文", "x" * 82])
def test_is_valid_slug_bad(bad: str):
    assert not is_valid_slug(bad)


async def test_resolve_conflict_no_conflict():
    async def exists(s: str) -> bool:
        return False
    result = await resolve_slug_conflict("foo", exists)
    assert result == "foo"


async def test_resolve_conflict_appends_suffix():
    taken = {"foo", "foo-2", "foo-3"}

    async def exists(s: str) -> bool:
        return s in taken

    result = await resolve_slug_conflict("foo", exists)
    assert result == "foo-4"


async def test_resolve_conflict_rejects_invalid():
    async def exists(s: str) -> bool:
        return False
    with pytest.raises(ValueError):
        await resolve_slug_conflict("UPPER", exists)
