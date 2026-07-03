"""ChatSession 与 /chat 通道测试。

不走真实 claude / git：用 FakeRunner 注入脚本化输出，stub 掉 worktree 创建。覆盖：
- 首轮 --session-id、次轮 --resume；WRITE 权限
- 输出分段回发、仅尾段带 tag；空输出兜底文案；失败 → FAILED（不落 sid）
- local repo 建 worktree；无 repo 回退 cwd + 警告
- apply_user_message 在 CHATTING 时拒绝；cancel 吸收后续状态写入
- SessionManager：建 chat row、continue 分流续聊、chat 不占 pipeline 槽、cancel
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cc_fleet.config.schema import (
    AppConfig,
    ChatConfig,
    ClaudeConfig,
    LimitsConfig,
    RepoConfig,
    WecomConfig,
)
from cc_fleet.core import chat as chat_mod
from cc_fleet.core import repo as repo_module
from cc_fleet.core.chat import _EMPTY_OUTPUT_NOTICE, _NO_REPO, ChatSession
from cc_fleet.core.runners.base import AgentPermission, ClaudeRunResult
from cc_fleet.core.session_manager import SessionManager
from cc_fleet.core.state import SessionState
from cc_fleet.storage.db import Database
from cc_fleet.util.ids import extract_quote_context

from tests.conftest import FakeRunner


# ---------- fixtures / 工具 ----------


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def replies() -> list:
    return []


@pytest.fixture
def reply(replies: list):
    async def _reply(chatid: str, text: str) -> None:
        replies.append((chatid, text))

    return _reply


def _cfg(tmp_path: Path, *, default_cwd: Path | None = None, chat_max: int = 4) -> AppConfig:
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir(exist_ok=True)
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        repos=[
            RepoConfig(name="repo-a", aliases=["a"], path=repo_a, default_branch="main")
        ],
        limits=LimitsConfig(max_concurrent_sessions=4),
        chat=ChatConfig(default_cwd=default_cwd, max_concurrent=chat_max, turn_timeout_sec=30),
    )


@pytest.fixture(autouse=True)
def stub_worktree(monkeypatch: pytest.MonkeyPatch):
    """stub git worktree 创建，让 chat 建 worktree 跑通而无需真仓库。"""
    created: list = []

    async def fake_fetch(_root: Path, _branch: str) -> None:
        pass

    async def fake_create(_root: Path, path: Path, branch: str, base: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)
        created.append((str(path), branch, base))

    monkeypatch.setattr(repo_module, "fetch_default_branch", fake_fetch)
    monkeypatch.setattr(repo_module, "create_worktree", fake_create)
    return created


def _runner(text: str = "回复", *, calls: list | None = None, **overrides) -> FakeRunner:
    """构造记录 kwargs 的 FakeRunner；overrides 覆盖 exit_code/result_is_error/timed_out/session_id。"""

    async def stub(**kwargs) -> ClaudeRunResult:
        if calls is not None:
            calls.append(kwargs)
        return ClaudeRunResult(
            exit_code=overrides.get("exit_code", 0),
            session_id=overrides.get("session_id", kwargs.get("resume_from") or kwargs["session_id"]),
            text_output=text,
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail=overrides.get("stderr_tail", ""),
            timed_out=overrides.get("timed_out", False),
            result_is_error=overrides.get("result_is_error", False),
        )

    return FakeRunner(stub)


async def _wait_state(db: Database, slug: str, target: SessionState, timeout: float = 5.0) -> dict:
    for _ in range(int(timeout / 0.01)):
        row = await db.get_session(slug)
        if row and row["state"] == target.value:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"chat {slug} 未在 {timeout}s 内进入 {target}")


# ---------- ChatSession 单元 ----------


async def test_first_turn_session_id_then_resume(db, tmp_path, reply):
    cfg = _cfg(tmp_path)
    calls: list = []
    chat = ChatSession(db=db, config=cfg, reply=reply, repo_cfg=cfg.repos[0])
    chat.runner = _runner("你好呀", calls=calls, session_id="sid-1")
    await chat.create_row(text="hi", chatid="c1", userid="u1")

    await chat.run_turn()
    # 首轮：resume_from=None（用 --session-id），WRITE 权限
    assert calls[0]["resume_from"] is None
    assert calls[0]["permission"] is AgentPermission.WRITE
    assert calls[0]["extra_env"]["CC_FLEET_WORKTREE"]
    row = await db.get_session(chat.slug)
    assert row["state"] == SessionState.CHAT_AWAITING.value
    assert row["claude_session_id"] == "sid-1"

    assert await chat.apply_user_message("再说说") is True
    await chat.run_turn()
    # 次轮：resume_from = 上轮捕获的 sid
    assert calls[1]["resume_from"] == "sid-1"


async def test_worktree_created_for_local_repo(db, tmp_path, reply, stub_worktree):
    cfg = _cfg(tmp_path)
    chat = ChatSession(db=db, config=cfg, reply=reply, repo_cfg=cfg.repos[0])
    chat.runner = _runner("hi")
    await chat.create_row(text="你好", chatid="c1", userid="u1")
    await chat.run_turn()
    row = await db.get_session(chat.slug)
    assert row["worktree_path"].endswith(f"repo-a-worktrees/{chat.slug}")
    assert row["branch"] == f"chat/{chat.slug}"
    assert stub_worktree and stub_worktree[0][1] == f"chat/{chat.slug}"


async def test_no_repo_runs_in_fallback_cwd(db, tmp_path, reply, stub_worktree):
    cfg = _cfg(tmp_path)
    fb = tmp_path / "fallback"
    chat = ChatSession(db=db, config=cfg, reply=reply, repo_cfg=None, fallback_cwd=fb)
    chat.runner = _runner("hi")
    await chat.create_row(text="你好", chatid="c1", userid="u1")
    await chat.run_turn()
    row = await db.get_session(chat.slug)
    assert row["worktree_path"] == str(fb)
    assert row["branch"] is None
    assert row["repo"] == _NO_REPO
    assert stub_worktree == []  # 无 repo 不建 worktree


async def test_forward_splits_and_tags_last_only(db, tmp_path, replies, reply):
    cfg = _cfg(tmp_path)
    long_text = "\n\n".join("A" + "x" * 3000 for _ in range(3))  # > 4000，分段
    chat = ChatSession(db=db, config=cfg, reply=reply, repo_cfg=cfg.repos[0])
    # 真实 claude_session_id 是 hex uuid；tag 的 sid 段要求 [0-9a-f-]{8,} 才可被反解析。
    chat.runner = _runner(long_text, session_id="deadbeefcafe0001")
    display = await chat.create_row(text="你好", chatid="c1", userid="u1")
    await chat.run_turn()
    outs = [t for (_c, t) in replies]
    assert len(outs) >= 2
    tagged = [t for t in outs if "[session:" in t]
    assert len(tagged) == 1  # 只有最后一段带 tag
    assert "[session:" in outs[-1]
    assert extract_quote_context(outs[-1]).slug == display


async def test_empty_output_notice(db, tmp_path, replies, reply):
    cfg = _cfg(tmp_path)
    chat = ChatSession(db=db, config=cfg, reply=reply, repo_cfg=cfg.repos[0])
    chat.runner = _runner("")  # claude 无文本输出
    await chat.create_row(text="你好", chatid="c1", userid="u1")
    await chat.run_turn()
    outs = [t for (_c, t) in replies]
    assert any(_EMPTY_OUTPUT_NOTICE in t for t in outs)
    assert (await db.get_session(chat.slug))["state"] == SessionState.CHAT_AWAITING.value


async def test_run_failure_sets_failed_and_no_sid(db, tmp_path, replies, reply):
    cfg = _cfg(tmp_path)
    chat = ChatSession(db=db, config=cfg, reply=reply, repo_cfg=cfg.repos[0])
    chat.runner = _runner("", exit_code=1, stderr_tail="boom")
    await chat.create_row(text="你好", chatid="c1", userid="u1")
    await chat.run_turn()
    row = await db.get_session(chat.slug)
    assert row["state"] == SessionState.FAILED.value
    # 失败不落 claude_session_id：首轮重试从干净会话开始
    assert row["claude_session_id"] is None
    assert any("❌" in t for (_c, t) in replies)


async def test_apply_user_message_rejected_while_chatting(db, tmp_path, reply):
    cfg = _cfg(tmp_path)
    chat = ChatSession(db=db, config=cfg, reply=reply, repo_cfg=cfg.repos[0])
    chat.runner = _runner("hi")
    await chat.create_row(text="你好", chatid="c1", userid="u1")
    # 建 row 后初始 state=CHATTING（首轮尚未跑）
    assert (await db.get_session(chat.slug))["state"] == SessionState.CHATTING.value
    assert await chat.apply_user_message("插话") is False


async def test_cancel_absorbs_subsequent_state(db, tmp_path, reply):
    cfg = _cfg(tmp_path)
    chat = ChatSession(db=db, config=cfg, reply=reply, repo_cfg=cfg.repos[0])
    chat.runner = _runner("hi")
    await chat.create_row(text="你好", chatid="c1", userid="u1")
    await chat.cancel()
    assert (await db.get_session(chat.slug))["state"] == SessionState.CANCELLED.value
    # 软取消：即便随后又跑了一轮，CANCELLED 也不被覆盖（_set_state 吸收）
    await chat.run_turn()
    assert (await db.get_session(chat.slug))["state"] == SessionState.CANCELLED.value


# ---------- SessionManager 集成 ----------


async def _slug_of(db: Database, display: str) -> str:
    row = await db.get_session_by_display_slug(display)
    assert row is not None
    return row["slug"]


def _patch_mgr_runner(monkeypatch, text="回复", calls: list | None = None):
    monkeypatch.setattr(
        chat_mod, "get_runner", lambda *a, **k: _runner(text, calls=calls)
    )


async def test_new_chat_session_runs_to_awaiting(db, tmp_path, reply, replies, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_mgr_runner(monkeypatch, text="你好呀")
    mgr = SessionManager(db, cfg, reply)
    display, note = await mgr.new_chat_session(
        repo_cfg=cfg.repos[0], text="hi", chatid="c1", userid="u1"
    )
    assert note is None  # 有 repo，无回退警告
    internal = await _slug_of(db, display)
    row = await _wait_state(db, internal, SessionState.CHAT_AWAITING)
    assert row["session_kind"] == "chat"
    assert any("你好呀" in t for (_c, t) in replies)
    await mgr.shutdown()
    # chat 完全不碰 pipeline 并发槽 / pending
    assert mgr._slot._value == cfg.limits.max_concurrent_sessions
    assert mgr._pending == 0


async def test_new_chat_session_no_repo_note_and_fallback(db, tmp_path, reply, monkeypatch):
    fb = tmp_path / "fb"
    cfg = _cfg(tmp_path, default_cwd=fb)
    _patch_mgr_runner(monkeypatch)
    mgr = SessionManager(db, cfg, reply)
    display, note = await mgr.new_chat_session(
        repo_cfg=None, text="hi", chatid="c1", userid="u1"
    )
    assert note is not None and str(fb) in note
    internal = await _slug_of(db, display)
    row = await _wait_state(db, internal, SessionState.CHAT_AWAITING)
    assert row["worktree_path"] == str(fb)
    assert row["repo"] == _NO_REPO
    await mgr.shutdown()


async def test_continue_session_routes_chat_and_resumes(db, tmp_path, reply, monkeypatch):
    cfg = _cfg(tmp_path)
    calls: list = []
    _patch_mgr_runner(monkeypatch, text="ok", calls=calls)
    mgr = SessionManager(db, cfg, reply)
    display, _ = await mgr.new_chat_session(
        repo_cfg=cfg.repos[0], text="hi", chatid="c1", userid="u1"
    )
    internal = await _slug_of(db, display)
    await _wait_state(db, internal, SessionState.CHAT_AWAITING)

    # 引用回复续聊：continue_session 用 display slug，应分流到 _continue_chat
    ok = await mgr.continue_session(slug=display, text="继续", quote_text=None)
    assert ok is True
    await _wait_state(db, internal, SessionState.CHAT_AWAITING)
    # 两轮都跑了（首轮 + 续聊）
    assert len(calls) >= 2
    await mgr.shutdown()


async def test_cancel_chat_via_manager(db, tmp_path, reply, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_mgr_runner(monkeypatch)
    mgr = SessionManager(db, cfg, reply)
    display, _ = await mgr.new_chat_session(
        repo_cfg=cfg.repos[0], text="hi", chatid="c1", userid="u1"
    )
    internal = await _slug_of(db, display)
    await _wait_state(db, internal, SessionState.CHAT_AWAITING)
    assert await mgr.cancel(display) is True
    await _wait_state(db, internal, SessionState.CANCELLED)
    await mgr.shutdown()


# ---------- 端到端：经 App._on_message 走完整链路 ----------


async def test_end_to_end_chat_via_app(db, tmp_path, monkeypatch):
    """@repo /chat → 建会话 → 转发输出；再引用回复 → 续聊第二轮。全链路真 SessionManager。"""
    from cc_fleet.app import App
    from cc_fleet.bot.base import BotRunner
    from cc_fleet.bot.message import IncomingMessage

    cfg = _cfg(tmp_path)
    calls: list = []
    _patch_mgr_runner(monkeypatch, text="这是 claude 的回答", calls=calls)

    replies: list = []

    class _Bot(BotRunner):
        def __init__(self) -> None:
            super().__init__(on_message=lambda _: None)

        async def reply(self, chatid: str, text: str) -> None:
            replies.append((chatid, text))

        async def run_forever(self) -> None:
            pass

    app = App(cfg)
    app.db = db
    bot = _Bot()
    app._bot = bot
    app._manager = SessionManager(db, cfg, bot.reply)

    # 1) @a /chat（repo-a 的 alias 是 a）
    await app._on_message(
        IncomingMessage(text="@a /chat 项目入口在哪", quote_text="", chatid="c1", userid="u1")
    )
    ack = replies[0][1]
    assert "已开始对话" in ack
    display = extract_quote_context(ack).slug
    assert display and display.startswith("chat-")
    internal = await _slug_of(db, display)
    await _wait_state(db, internal, SessionState.CHAT_AWAITING)
    assert any("这是 claude 的回答" in t for (_c, t) in replies)

    # 2) 引用带 tag 的输出消息续聊
    tag_msg = [t for (_c, t) in replies if "[session:" in t][-1]
    await app._on_message(
        IncomingMessage(text="那 dispatcher 呢", quote_text=tag_msg, chatid="c1", userid="u1")
    )
    await _wait_state(db, internal, SessionState.CHAT_AWAITING)
    assert len(calls) >= 2  # 首轮 + 续聊两轮
    await app._manager.shutdown()
