"""异步 SQLite 访问层。

主要职责：
- 初始化数据库 / 跑 migrations
- 提供 sessions / messages / events 三张表的 CRUD 原语

后续 session.py、dispatcher.py 都通过这一层读写状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from ..util.time import now_local_iso
from .migrations import MIGRATIONS


def _now() -> str:
    return now_local_iso()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database 未连接")
        return self._conn

    async def _migrate(self) -> None:
        # schema_version 表本身就是第一条 migration，保证存在后再判定
        await self.conn.execute(MIGRATIONS[0])
        await self.conn.commit()
        cur = await self.conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        current = row[0] if row and row[0] is not None else 0
        for idx, sql in enumerate(MIGRATIONS[1:], start=1):
            if idx <= current:
                continue
            await self.conn.execute(sql)
            await self.conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (idx, _now()),
            )
            await self.conn.commit()

    # ---------- sessions ----------

    async def insert_session(self, fields: dict[str, Any]) -> None:
        ts = _now()
        fields.setdefault("created_at", ts)
        fields.setdefault("updated_at", ts)
        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        await self.conn.execute(
            f"INSERT INTO sessions({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        await self.conn.commit()

    async def update_session(self, slug: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(
            f"UPDATE sessions SET {set_clause} WHERE slug = ?",
            (*fields.values(), slug),
        )
        await self.conn.commit()

    async def get_session(self, slug: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM sessions WHERE slug = ?", (slug,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_session_by_display_slug(self, display_slug: str) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM sessions WHERE display_slug = ?", (display_slug,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def display_slug_exists(self, display_slug: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM sessions WHERE display_slug = ?", (display_slug,)
        )
        return await cur.fetchone() is not None

    async def list_sessions(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            cur = await self.conn.execute(
                "SELECT * FROM sessions WHERE state = ? ORDER BY created_at DESC", (state,)
            )
        else:
            cur = await self.conn.execute("SELECT * FROM sessions ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]

    async def slug_exists(self, slug: str) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM sessions WHERE slug = ?", (slug,))
        return await cur.fetchone() is not None

    async def session_exists_with_origin(self, origin_chat_slug: str) -> bool:
        """是否已有某条 pipeline session 由该 chat（内部 slug）转入（/dev handoff）。

        用于 ``new_pipeline_from_chat`` 拒绝对同一 chat 二次 /dev；存储层还有部分唯一索引
        ``idx_sessions_origin_chat`` 作为并发原子兜底。
        """
        cur = await self.conn.execute(
            "SELECT 1 FROM sessions WHERE origin_chat_slug = ?", (origin_chat_slug,)
        )
        return await cur.fetchone() is not None

    async def find_recent_open_chat(self, chatid: str) -> dict[str, Any] | None:
        """取该 chatid 下最近一个「活跃」chat 会话，供私聊窗口内免引用自动续聊判定。

        - 只认活跃态（chatting / chat_awaiting）；已失败 / 取消 / 转开发的 chat 不返回，
          避免一句无关消息静默复活久远或失败的会话（那些仍需用户显式引用）。字面量
          'chatting'/'chat_awaiting' 对应 ``SessionState.CHATTING/CHAT_AWAITING``。
        - ``last_reply_ts``：该会话最后一条 ``direction='out'`` 消息的 ts（= 机器人最后
          回复时刻）；若尚未回过（刚 chatting 未产出）退回 ``updated_at`` 作锚点。
        - 私聊建 row 时 chatid 落的是 ``msg.chatid or userid``（私聊即 userid），故这里按
          chatid 精确匹配即可把该用户私聊会话与其群聊会话（chatid=群 id）隔离。

        返回含 ``slug`` 与 ``last_reply_ts`` 的 dict；无活跃 chat 时 None。窗口是否命中由
        调用方（``App`` 闭包，持有 config）用 ``auto_continue_window_sec`` 判定。
        """
        cur = await self.conn.execute(
            "SELECT s.slug AS slug,"
            "       COALESCE(MAX(m.ts), s.updated_at) AS last_reply_ts "
            "FROM sessions s "
            "LEFT JOIN messages m"
            "  ON m.session_slug = s.slug AND m.direction = 'out' "
            "WHERE s.chatid = ?"
            "  AND s.session_kind = 'chat'"
            "  AND s.state IN ('chatting', 'chat_awaiting') "
            "GROUP BY s.slug "
            "ORDER BY last_reply_ts DESC "
            "LIMIT 1",
            (chatid,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ---------- messages ----------

    async def add_message(
        self,
        session_slug: str,
        direction: str,
        text: str,
        quote_text: str | None = None,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO messages(session_slug, direction, text, quote_text, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_slug, direction, text, quote_text, _now()),
        )
        await self.conn.commit()

    async def list_messages(self, session_slug: str) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM messages WHERE session_slug = ? ORDER BY id ASC",
            (session_slug,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ---------- events ----------

    async def add_event(self, session_slug: str, kind: str, payload: Any = None) -> None:
        await self.conn.execute(
            "INSERT INTO events(session_slug, kind, payload_json, ts) VALUES (?, ?, ?, ?)",
            (
                session_slug,
                kind,
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                _now(),
            ),
        )
        await self.conn.commit()

    async def list_events(
        self, session_slug: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """返回某个 session 的最近 N 条事件，倒序（最新在前）。

        排除 ``*.stream_event``：claude SDK 每个流式碎片（text_delta /
        input_json_delta 等）都会落一条 ``claude.stream_event``，单次 coding 阶段
        轻松数千条。这些碎片已由 ``claude.assistant`` / ``claude.user`` 聚合，
        前端也始终 filter 掉它们；从 SQL 层直接排除，避免 limit 配额被噪声吃光、
        导致早段 plan/coding 的可见事件被刷出窗口（HTTP 面板曾因此只能看到尾部）。
        """
        cur = await self.conn.execute(
            "SELECT * FROM events"
            " WHERE session_slug = ?"
            "   AND kind NOT LIKE '%.stream_event'"
            " ORDER BY id DESC LIMIT ?",
            (session_slug, limit),
        )
        return [dict(r) for r in await cur.fetchall()]
