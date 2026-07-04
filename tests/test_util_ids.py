"""util/ids: tag 格式化与引用上下文解析。"""

from __future__ import annotations

from cc_fleet.util.ids import (
    extract_quote_context,
    find_session_tag,
    format_session_tag,
)


def test_format_session_tag_slug_only():
    assert format_session_tag("add-login") == "[session: add-login]"


def test_format_session_tag_with_repo():
    assert format_session_tag("add-login", repo="feed-web") == "[session: add-login @feed-web]"


def test_format_session_tag_full():
    tag = format_session_tag(
        "add-login", repo="feed-web", claude_session_id="abcd1234-1111-2222-3333-444455556666"
    )
    assert tag == "[session: add-login @feed-web sid: abcd1234-1111-2222-3333-444455556666]"


def test_format_session_tag_skips_empty_repo():
    """传空字符串视为缺位，不渲染 @ 段。"""
    assert format_session_tag("x", repo="", claude_session_id="") == "[session: x]"


def test_extract_quote_context_full():
    ctx = extract_quote_context(
        "需要进一步确认：\n1. 用密码还是 OAuth？\n\n"
        "[session: add-login @feed-web sid: abcd1234-1111-2222-3333-444455556666]"
    )
    assert ctx.slug == "add-login"
    assert ctx.repo == "feed-web"
    assert ctx.claude_session_id == "abcd1234-1111-2222-3333-444455556666"


def test_extract_quote_context_slug_only_legacy():
    """旧格式 [session: foo] 应仍能解析。"""
    ctx = extract_quote_context("blah\n[session: foo]")
    assert ctx.slug == "foo"
    assert ctx.repo is None
    assert ctx.claude_session_id is None


def test_extract_quote_context_slug_and_repo():
    ctx = extract_quote_context("已完成。\n[session: dead @my-project]")
    assert ctx.slug == "dead"
    assert ctx.repo == "my-project"
    assert ctx.claude_session_id is None


def test_extract_quote_context_repo_only_tag():
    """初始 reply 阶段没有 slug，只有 [repo: ...] 也要能解析出 repo。"""
    ctx = extract_quote_context(
        "已收到需求，开始分析 @feed-web。当 plan 完成或需要确认时会再通知你。\n\n[repo: feed-web]"
    )
    assert ctx.slug is None
    assert ctx.repo == "feed-web"


def test_extract_quote_context_session_tag_wins_over_repo_only():
    """同一段文本里如果两种 tag 都有，优先取 session tag 里的全套信息。"""
    ctx = extract_quote_context("[session: live @demo]\n[repo: other]")
    assert ctx.slug == "live"
    assert ctx.repo == "demo"


def test_extract_quote_context_empty():
    assert extract_quote_context("").slug is None
    assert extract_quote_context("没有标签的文本").slug is None


def test_find_session_tag_returns_raw_substring():
    text = "plan 已就绪 ✅\n\n[session: add-login @feed-web sid: abcd1234-1111-2222-3333-444455556666]"
    assert find_session_tag(text) == "[session: add-login @feed-web sid: abcd1234-1111-2222-3333-444455556666]"


def test_find_session_tag_repo_only():
    assert find_session_tag("已收到需求 @feed-web\n\n[repo: feed-web]") == "[repo: feed-web]"


def test_find_session_tag_none_when_absent():
    assert find_session_tag("普通文本，无标签") is None
    assert find_session_tag("") is None
