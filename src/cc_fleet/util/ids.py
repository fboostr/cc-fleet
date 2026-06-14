"""标识符相关工具：UUID 生成、临时 slug、SESSION 标签格式化与解析。"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass

from .time import now_local_compact

# 嵌入到机器人发送消息里的 session 标签。
# 完整格式：[session: <slug> @<repo> sid: <uuid>]
# 后两段可选，便于：
#   - 渐进式渲染（claude_session_id 出来后再补 sid；slug 字段在首次 ack 时为
#     internal slug，plan 拿到可读 slug 后切到 display_slug）
#   - 兼容历史消息（只有 [session: <slug>] 这种旧格式仍能解析）
SESSION_TAG_PATTERN = re.compile(
    r"\[session:\s*([a-z0-9][a-z0-9-]{2,80})"
    r"(?:\s+@([\w.-]+))?"
    r"(?:\s+sid:\s*([0-9a-f-]{8,}))?"
    r"\s*\]",
    re.IGNORECASE,
)

# 历史 ack（display_slug 时代之前的初始 reply）只挂 [repo: ...]，没有 slug。
# 生成端已切到 format_session_tag(internal_slug, repo=...)，但解析端仍保留兼容：
# 用户引用 IM 里早期发出的消息时还能恢复出 repo。
REPO_ONLY_TAG_PATTERN = re.compile(r"\[repo:\s*([\w.-]+)\s*\]", re.IGNORECASE)


@dataclass
class QuoteContext:
    """从用户引用文本中解析出的路由上下文。所有字段都可能为 None。"""

    slug: str | None = None
    repo: str | None = None
    claude_session_id: str | None = None


def new_uuid() -> str:
    """生成 UUIDv4 字符串，用于 claude --session-id。"""
    return str(uuid.uuid4())


def new_internal_slug() -> str:
    """生成 session 的内部 slug：可读且唯一，用作 DB 主键与分支名后缀。"""
    return f"req-{now_local_compact()}-{secrets.token_hex(2)}"


def format_session_tag(
    slug: str,
    repo: str | None = None,
    claude_session_id: str | None = None,
) -> str:
    """格式化为一行嵌入消息文本，让用户引用回来时可被反向解析。

    渐进式：repo 与 claude_session_id 都可选；缺位则只输出已有字段。
    """
    parts = [f"[session: {slug}"]
    if repo:
        parts.append(f" @{repo}")
    if claude_session_id:
        parts.append(f" sid: {claude_session_id}")
    parts.append("]")
    return "".join(parts)


def extract_quote_context(text: str) -> QuoteContext:
    """从任意文本（通常是用户引用的 quote.text）中提取路由上下文。

    优先匹配 SESSION_TAG_PATTERN（完整 tag）；未命中再尝试 REPO_ONLY_TAG_PATTERN。
    任一字段缺失为 None。
    """
    if not text:
        return QuoteContext()

    m = SESSION_TAG_PATTERN.search(text)
    if m is not None:
        return QuoteContext(
            slug=m.group(1).lower() if m.group(1) else None,
            repo=m.group(2) if m.group(2) else None,
            claude_session_id=m.group(3).lower() if m.group(3) else None,
        )

    rm = REPO_ONLY_TAG_PATTERN.search(text)
    if rm is not None:
        return QuoteContext(repo=rm.group(1))

    return QuoteContext()
