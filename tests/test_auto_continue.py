"""私聊「窗口内免引用自动续聊」的底层验证：

- ``Database.find_recent_open_chat``：只取活跃 chat、取最近、按 chatid 隔离、last_reply_ts
  来自最后一条 out 消息（无则退回 updated_at）。
- ``app._within_reply_window``：窗口内 / 超窗 / 空 / 脏 ts / 无时区的判定（保守不续）。

dispatcher 层的路由分支在 ``tests/test_dispatcher.py`` 覆盖，这里补上被它依赖的两块基础件。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cc_fleet.app import _within_reply_window
from cc_fleet.core.state import SessionState
from cc_fleet.storage.db import Database


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.db")
    await d.connect()
    yield d
    await d.close()


async def _insert_chat(
    db: Database,
    slug: str,
    *,
    chatid: str,
    state: SessionState = SessionState.CHAT_AWAITING,
    userid: str = "u",
    session_kind: str = "chat",
) -> None:
    await db.insert_session(
        {
            "slug": slug,
            "repo": "feed-web",
            "state": state.value,
            "default_branch": "main",
            "initial_request": "hi",
            "session_kind": session_kind,
            "chatid": chatid,
            "userid": userid,
        }
    )


async def _insert_out(db: Database, slug: str, ts: str) -> None:
    """直插一条带指定 ts 的 out 消息（add_message 只会用当前时刻，测排序需自定义 ts）。"""
    await db.conn.execute(
        "INSERT INTO messages(session_slug, direction, text, ts) VALUES (?, ?, ?, ?)",
        (slug, "out", "reply", ts),
    )
    await db.conn.commit()


# ---------- Database.find_recent_open_chat ----------


async def test_find_returns_active_chat_with_last_out_ts(db: Database):
    await _insert_chat(db, "chat-1", chatid="u")
    await _insert_out(db, "chat-1", "2026-07-06T10:00:00+08:00")
    await _insert_out(db, "chat-1", "2026-07-06T10:05:00+08:00")

    hit = await db.find_recent_open_chat("u")
    assert hit is not None
    assert hit["slug"] == "chat-1"
    # last_reply_ts 取最后一条 out（最大 ts）
    assert hit["last_reply_ts"] == "2026-07-06T10:05:00+08:00"


async def test_find_ignores_non_active_and_non_chat(db: Database):
    """failed / cancelled 的 chat、以及 pipeline 会话都不算"活跃 chat"。"""
    await _insert_chat(db, "chat-failed", chatid="u", state=SessionState.FAILED)
    await _insert_chat(db, "chat-cancelled", chatid="u", state=SessionState.CANCELLED)
    await _insert_chat(
        db, "pipe-x", chatid="u", state=SessionState.DEVELOPING, session_kind="pipeline"
    )
    assert await db.find_recent_open_chat("u") is None


async def test_find_picks_most_recent_by_last_reply(db: Database):
    await _insert_chat(db, "chat-old", chatid="u")
    await _insert_out(db, "chat-old", "2026-07-06T09:00:00+08:00")
    await _insert_chat(db, "chat-new", chatid="u")
    await _insert_out(db, "chat-new", "2026-07-06T11:00:00+08:00")

    hit = await db.find_recent_open_chat("u")
    assert hit is not None and hit["slug"] == "chat-new"


async def test_find_isolates_by_chatid(db: Database):
    """私聊按 chatid（= userid）隔离：查 u1 不应命中 u2 的会话。"""
    await _insert_chat(db, "chat-u1", chatid="u1")
    await _insert_out(db, "chat-u1", "2026-07-06T10:00:00+08:00")
    await _insert_chat(db, "chat-u2", chatid="u2")
    await _insert_out(db, "chat-u2", "2026-07-06T12:00:00+08:00")

    hit = await db.find_recent_open_chat("u1")
    assert hit is not None and hit["slug"] == "chat-u1"


async def test_find_falls_back_to_updated_at_without_out(db: Database):
    """刚 chatting 尚未回过（无 out 消息）→ last_reply_ts 退回 updated_at（非空）。"""
    await _insert_chat(db, "chat-fresh", chatid="u", state=SessionState.CHATTING)
    hit = await db.find_recent_open_chat("u")
    assert hit is not None and hit["slug"] == "chat-fresh"
    assert hit["last_reply_ts"]  # 非空（= updated_at）


async def test_find_none_when_empty(db: Database):
    assert await db.find_recent_open_chat("nobody") is None


# ---------- app._within_reply_window ----------


def _aware_now() -> datetime:
    return datetime(2026, 7, 6, 12, 0, 0).astimezone()


def test_within_window_recent_true():
    now = _aware_now()
    last = (now - timedelta(minutes=10)).isoformat()
    assert _within_reply_window(last, 1800, now) is True


def test_within_window_expired_false():
    now = _aware_now()
    last = (now - timedelta(minutes=40)).isoformat()
    assert _within_reply_window(last, 1800, now) is False


def test_within_window_boundary_inclusive():
    now = _aware_now()
    last = (now - timedelta(seconds=1800)).isoformat()
    assert _within_reply_window(last, 1800, now) is True


def test_within_window_empty_or_malformed_false():
    assert _within_reply_window(None, 1800) is False
    assert _within_reply_window("", 1800) is False
    assert _within_reply_window("not-a-timestamp", 1800) is False


def test_within_window_naive_ts_is_conservative():
    """历史脏数据：ts 无时区而 now 有 → 不可比，保守判 False（回落开新）。"""
    now = _aware_now()
    assert _within_reply_window("2026-07-06T11:55:00", 1800, now) is False
