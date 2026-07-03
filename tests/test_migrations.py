"""storage/migrations.py：schema 迁移全量应用 + 二次连接幂等。

经 Database.connect() 跑真实迁移路径（_migrate），而非直接拼 SQL。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_fleet.storage.db import Database
from cc_fleet.storage.migrations import MIGRATIONS


async def _columns(db: Database, table: str) -> set[str]:
    cur = await db.conn.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in await cur.fetchall()}


async def _table_exists(db: Database, table: str) -> bool:
    cur = await db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cur.fetchone() is not None


async def test_fresh_connect_applies_all_migrations(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    await db.connect()
    try:
        # 三张主表 + 版本表都建好
        for t in ("schema_version", "sessions", "messages", "events"):
            assert await _table_exists(db, t), f"缺表 {t}"
        # 版本号推进到最后一条迁移（MIGRATIONS[0] 是 schema_version 表，从 idx=1 开始记版本）
        cur = await db.conn.execute("SELECT MAX(version) FROM schema_version")
        assert (await cur.fetchone())[0] == len(MIGRATIONS) - 1
    finally:
        await db.close()


async def test_sessions_has_all_altered_columns(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    await db.connect()
    try:
        cols = await _columns(db, "sessions")
        # 后续 ALTER 增补的列都应在
        for c in (
            "failed_phase",
            "reviewer_session_id",
            "plan_review_rounds",
            "code_review_rounds",
            "review_override",
            "session_kind",
            "origin_chat_slug",
        ):
            assert c in cols, f"sessions 缺列 {c}"
    finally:
        await db.close()


async def test_origin_chat_slug_unique_index(tmp_path: Path):
    """部分唯一索引 idx_sessions_origin_chat：同一非空 origin_chat_slug 只能有一行。"""
    import sqlite3

    db = Database(tmp_path / "state.db")
    await db.connect()
    try:
        base = {
            "display_slug": None,
            "repo": "r",
            "state": "new",
            "default_branch": "main",
            "initial_request": "hi",
            "chatid": "c",
            "userid": "u",
        }
        await db.insert_session({**base, "slug": "req-1", "origin_chat_slug": "chat-x"})
        assert await db.session_exists_with_origin("chat-x") is True
        assert await db.session_exists_with_origin("chat-y") is False
        # 同一 chat 再转一次 → 唯一索引拦截
        with pytest.raises(sqlite3.IntegrityError):
            await db.insert_session(
                {**base, "slug": "req-2", "origin_chat_slug": "chat-x"}
            )
        # 多行 origin_chat_slug 为 NULL（普通新需求）不受唯一约束影响
        await db.insert_session({**base, "slug": "req-3"})
        await db.insert_session({**base, "slug": "req-4"})
    finally:
        await db.close()


async def test_session_kind_defaults_to_pipeline(tmp_path: Path):
    """不带 session_kind 的插入（旧代码路径 / 老行）应回填默认 'pipeline'。"""
    db = Database(tmp_path / "state.db")
    await db.connect()
    try:
        await db.insert_session(
            {
                "slug": "req-x",
                "display_slug": None,
                "repo": "r",
                "state": "new",
                "default_branch": "main",
                "initial_request": "hi",
                "chatid": "c",
                "userid": "u",
            }
        )
        row = await db.get_session("req-x")
        assert row is not None and row["session_kind"] == "pipeline"
    finally:
        await db.close()


async def test_reconnect_is_idempotent(tmp_path: Path):
    path = tmp_path / "state.db"

    db1 = Database(path)
    await db1.connect()
    cur = await db1.conn.execute("SELECT COUNT(*) FROM schema_version")
    count1 = (await cur.fetchone())[0]
    await db1.close()

    # 同一文件再次连接：所有迁移 idx <= current 应被跳过，不重复插版本、不报错
    db2 = Database(path)
    await db2.connect()
    try:
        cur = await db2.conn.execute("SELECT COUNT(*) FROM schema_version")
        count2 = (await cur.fetchone())[0]
        cur = await db2.conn.execute("SELECT MAX(version) FROM schema_version")
        maxv = (await cur.fetchone())[0]
        assert count2 == count1  # 未重复写版本行
        assert maxv == len(MIGRATIONS) - 1
    finally:
        await db2.close()
