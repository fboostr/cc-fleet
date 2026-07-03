"""SQLite 表结构。每条 migration 是一段 SQL，按顺序执行。"""

MIGRATIONS: list[str] = [
    # v1：sessions / messages / events 三张主表
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        slug                 TEXT PRIMARY KEY,            -- 内部主键，初始为 tmp-xxx
        display_slug         TEXT UNIQUE,                 -- 用户可见 slug（plan 阶段 claude 返回）
        repo                 TEXT NOT NULL,
        state                TEXT NOT NULL,
        claude_session_id    TEXT,
        worktree_path        TEXT,
        branch               TEXT,
        default_branch       TEXT NOT NULL,
        initial_request      TEXT NOT NULL,
        chatid               TEXT,
        userid               TEXT,
        clarify_rounds       INTEGER NOT NULL DEFAULT 0,
        last_error           TEXT,
        mr_url               TEXT,
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sessions_display_slug ON sessions(display_slug);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sessions_repo  ON sessions(repo);
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_slug  TEXT NOT NULL,
        direction     TEXT NOT NULL,            -- in / out
        text          TEXT NOT NULL,
        quote_text    TEXT,
        ts            TEXT NOT NULL,
        FOREIGN KEY (session_slug) REFERENCES sessions(slug)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_slug);
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_slug  TEXT NOT NULL,
        kind          TEXT NOT NULL,
        payload_json  TEXT,
        ts            TEXT NOT NULL,
        FOREIGN KEY (session_slug) REFERENCES sessions(slug)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_slug);
    """,
    # sessions 加 failed_phase。失败/超时时记录当时所处阶段，供引用回复唤醒时
    # 决定 resume 回到哪个状态。老 row 该列为 NULL，由 session.py 用 last_error 启发兜底。
    """
    ALTER TABLE sessions ADD COLUMN failed_phase TEXT;
    """,
    # 独立 Reviewer 相关列：
    # - reviewer_session_id：Reviewer 专属 claude 会话 id（独立于 Coder 的 claude_session_id），
    #   首次审查时懒生成并持久化，后续审查 resume 该会话保持上下文连续。
    # - plan_review_rounds / code_review_rounds：「审查→Coder 修订」已发生的轮数，用于与
    #   reviewer.max_rounds 比较，决定是否继续审查、防止来回死循环。
    """
    ALTER TABLE sessions ADD COLUMN reviewer_session_id TEXT;
    """,
    """
    ALTER TABLE sessions ADD COLUMN plan_review_rounds INTEGER NOT NULL DEFAULT 0;
    """,
    """
    ALTER TABLE sessions ADD COLUMN code_review_rounds INTEGER NOT NULL DEFAULT 0;
    """,
    # 单需求级 review 覆盖：NULL=跟随 repo reviewer.enabled，1=强制开，0=强制关。
    # 来源：用户在需求文本里的 [review]/[review:off] 内联指令（见 core/dispatcher.py），
    # 仅对该 session 生效，覆盖仓库默认。老 row 该列为 NULL，行为与改动前完全一致。
    """
    ALTER TABLE sessions ADD COLUMN review_override INTEGER;
    """,
    # session 类型：pipeline（默认，plan→dev→MR 交付流水线）/ chat（/chat 自由对话通道，
    # 见 core/chat.py）。老 row 无该列，迁移后一律回填为 pipeline，行为与改动前完全一致。
    """
    ALTER TABLE sessions ADD COLUMN session_kind TEXT NOT NULL DEFAULT 'pipeline';
    """,
]
