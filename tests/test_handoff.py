"""/chat → pipeline handoff（/dev）测试。

覆盖 SessionManager.new_pipeline_from_chat 的编排与前置校验、Session._do_planning 的
handoff 首轮（--resume 复用 chat 会话）、原 chat 归档，以及经 App._on_message 的端到端链路。
不走真实 claude / git：chat 与 pipeline 各注入 FakeRunner，repo/mr 全 stub。
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
    PipelineConfig,
    RepoConfig,
    WecomConfig,
)
from cc_fleet.core import chat as chat_mod
from cc_fleet.core import mr as mr_module
from cc_fleet.core import repo as repo_module
from cc_fleet.core import session as session_mod
from cc_fleet.core.chat import _NO_REPO
from cc_fleet.core.runners.base import ClaudeRunResult
from cc_fleet.core.session_manager import SessionManager
from cc_fleet.core.state import SessionState
from cc_fleet.storage.db import Database
from cc_fleet.util.ids import extract_quote_context

from tests.conftest import FakeRunner, perm_mode

# chat 首轮返回的 claude_session_id：hex 形态，保证嵌进 session tag 后能被反解析（sid 段要求 hex）。
_CSID = "deadbeefcafe0001"


# ---------- fixtures ----------


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


def _cfg(tmp_path: Path) -> AppConfig:
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir(exist_ok=True)
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        pipeline=PipelineConfig(plan_timeout_sec=10, dev_timeout_sec=10, max_clarify_rounds=2),
        repos=[
            RepoConfig(name="repo-a", aliases=["a"], path=repo_a, default_branch="main")
        ],
        limits=LimitsConfig(max_concurrent_sessions=4),
        chat=ChatConfig(max_concurrent=4, turn_timeout_sec=30),
    )


@pytest.fixture(autouse=True)
def stub_repo_and_mr(monkeypatch: pytest.MonkeyPatch):
    """统一 stub git / mr：让 chat 建 worktree 与 pipeline _do_new/_do_mr 跑通而无需真仓库。"""

    async def fake_fetch(_root: Path, _branch: str) -> None:
        pass

    async def fake_create_worktree(_root: Path, path: Path, _branch: str, _base: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    async def fake_has_commits_ahead(_path: Path, _base: str) -> bool:
        return True

    async def fake_mr_create(**_kwargs) -> str:
        return "https://gitlab/example/-/merge_requests/1"

    monkeypatch.setattr(repo_module, "fetch_default_branch", fake_fetch)
    monkeypatch.setattr(repo_module, "create_worktree", fake_create_worktree)
    monkeypatch.setattr(repo_module, "has_commits_ahead", fake_has_commits_ahead)
    monkeypatch.setattr(mr_module, "create_mr_via_push", fake_mr_create)


def _patch_chat_runner(monkeypatch, *, text: str = "讨论回复", csid: str = _CSID) -> None:
    async def stub(**kwargs) -> ClaudeRunResult:
        return ClaudeRunResult(
            exit_code=0,
            session_id=csid,
            text_output=text,
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(chat_mod, "get_runner", lambda *a, **k: FakeRunner(stub))


def _patch_pipeline_runner(
    monkeypatch,
    *,
    calls: list | None = None,
    plan_text: str = "plan ready\n\nSLUG: feat\nSTATUS: READY",
    dev_text: str = "完成 ✅",
) -> None:
    async def stub(**kwargs) -> ClaudeRunResult:
        if calls is not None:
            calls.append(kwargs)
        mode = perm_mode(kwargs)
        text = plan_text if mode == "plan" else dev_text
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output=text,
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))


async def _wait_state(db: Database, slug: str, target: SessionState, timeout: float = 5.0) -> dict:
    for _ in range(int(timeout / 0.01)):
        row = await db.get_session(slug)
        if row and row["state"] == target.value:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"session {slug} 未在 {timeout}s 内进入 {target}")


async def _make_chat_awaiting(db, cfg, reply, monkeypatch) -> tuple[SessionManager, str, str]:
    """建一个跑到 CHAT_AWAITING（csid 已落库）的 chat，返回 (mgr, display, internal)。"""
    _patch_chat_runner(monkeypatch)
    mgr = SessionManager(db, cfg, reply)
    display, _ = await mgr.new_chat_session(
        repo_cfg=cfg.repos[0], text="想加个 X 功能", chatid="c1", userid="u1"
    )
    row = await db.get_session_by_display_slug(display)
    internal = row["slug"]
    await _wait_state(db, internal, SessionState.CHAT_AWAITING)
    return mgr, display, internal


async def _insert_chat_row(
    db: Database,
    *,
    slug: str,
    repo: str = "repo-a",
    state: SessionState = SessionState.CHAT_AWAITING,
    csid: str | None = _CSID,
) -> None:
    await db.insert_session(
        {
            "slug": slug,
            "display_slug": slug,
            "repo": repo,
            "state": state.value,
            "claude_session_id": csid,
            "default_branch": "main",
            "initial_request": "聊过的需求",
            "chatid": "c1",
            "userid": "u1",
            "session_kind": "chat",
        }
    )


# ---------- happy path ----------


async def test_handoff_happy_path(db, tmp_path, reply, monkeypatch):
    cfg = _cfg(tmp_path)
    mgr, display, internal = await _make_chat_awaiting(db, cfg, reply, monkeypatch)

    # chat 已就绪 → 转开发
    _patch_pipeline_runner(monkeypatch)
    slug, repo_name, ahead, err = await mgr.new_pipeline_from_chat(
        chat_slug=display, supplement="记得补单测", chatid="c1", userid="u1"
    )
    assert err is None and slug is not None
    assert repo_name == "repo-a"

    prow = await _wait_state(db, slug, SessionState.COMPLETED)
    assert prow["session_kind"] == "pipeline"
    assert prow["origin_chat_slug"] == internal
    # 复用了 chat 的 claude 会话
    assert prow["claude_session_id"] == _CSID
    # 组装的 initial_request 带上了对话最初需求与补充说明
    assert "想加个 X 功能" in prow["initial_request"]
    assert "记得补单测" in prow["initial_request"]

    # 原 chat 已归档
    crow = await db.get_session(internal)
    assert crow["state"] == SessionState.CANCELLED.value
    assert "已转为开发任务" in (crow["last_error"] or "")
    await mgr.shutdown()


async def test_handoff_first_plan_resumes_chat_session(db, tmp_path, reply, monkeypatch):
    """handoff 首个 plan 轮应 --resume chat 的 csid，且 prompt 是「基于讨论规划」而非原样重述。"""
    cfg = _cfg(tmp_path)
    mgr, display, internal = await _make_chat_awaiting(db, cfg, reply, monkeypatch)

    calls: list = []
    _patch_pipeline_runner(monkeypatch, calls=calls)
    slug, *_ = await mgr.new_pipeline_from_chat(
        chat_slug=display, supplement="", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.COMPLETED)

    plan_calls = [c for c in calls if perm_mode(c) == "plan"]
    assert plan_calls, "应至少有一个 plan 轮"
    first_plan = plan_calls[0]
    assert first_plan["resume_from"] == _CSID  # 首轮就 resume（而非普通 pipeline 的 None）
    assert "规划" in first_plan["prompt"] and "讨论" in first_plan["prompt"]
    await mgr.shutdown()


async def test_handoff_consumes_pipeline_slot_not_chat(db, tmp_path, reply, monkeypatch):
    cfg = _cfg(tmp_path)
    mgr, display, _ = await _make_chat_awaiting(db, cfg, reply, monkeypatch)
    _patch_pipeline_runner(monkeypatch)
    slug, *_ = await mgr.new_pipeline_from_chat(
        chat_slug=display, supplement="", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.COMPLETED)
    await mgr.shutdown()
    # 跑完后 pipeline 槽全部归还、pending 归零；chat 槽从未被 handoff 借用
    assert mgr._slot._value == cfg.limits.max_concurrent_sessions
    assert mgr._chat_slot._value == cfg.chat.max_concurrent
    assert mgr._pending == 0


async def test_handoff_allowed_after_chat_cancelled(db, tmp_path, reply, monkeypatch):
    """chat 被 /cancel 后（csid 仍在、未转过）仍可 /dev 救活成开发任务。"""
    cfg = _cfg(tmp_path)
    mgr = SessionManager(db, cfg, reply)
    await _insert_chat_row(db, slug="chat-cxl", state=SessionState.CANCELLED)
    _patch_pipeline_runner(monkeypatch)
    slug, repo_name, _ahead, err = await mgr.new_pipeline_from_chat(
        chat_slug="chat-cxl", supplement="", chatid="c1", userid="u1"
    )
    assert err is None and slug is not None
    await _wait_state(db, slug, SessionState.COMPLETED)
    await mgr.shutdown()


async def test_origin_chat_archived_blocks_continue(db, tmp_path, reply, monkeypatch):
    cfg = _cfg(tmp_path)
    mgr, display, internal = await _make_chat_awaiting(db, cfg, reply, monkeypatch)
    _patch_pipeline_runner(monkeypatch)
    slug, *_ = await mgr.new_pipeline_from_chat(
        chat_slug=display, supplement="", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.COMPLETED)
    # 归档后再引用原 chat 续聊 → continue_session 应返回 False（chat 已 CANCELLED，非 open）
    ok = await mgr.continue_session(slug=display, text="接着聊", quote_text=None)
    assert ok is False
    await mgr.shutdown()


# ---------- 拒绝分支（都在 create_row 之前返回，不起 task） ----------


async def test_reject_chat_slug_not_found(db, tmp_path, reply):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(db, cfg, reply)
    slug, _repo, _ahead, err = await mgr.new_pipeline_from_chat(
        chat_slug="nope", supplement="", chatid="c1", userid="u1"
    )
    assert slug is None and err and "未找到" in err


async def test_reject_not_a_chat_session(db, tmp_path, reply):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(db, cfg, reply)
    # 一条普通 pipeline row（session_kind 默认 pipeline）
    await db.insert_session(
        {
            "slug": "req-plain",
            "display_slug": "req-plain",
            "repo": "repo-a",
            "state": "developing",
            "default_branch": "main",
            "initial_request": "x",
            "chatid": "c1",
            "userid": "u1",
        }
    )
    slug, _repo, _ahead, err = await mgr.new_pipeline_from_chat(
        chat_slug="req-plain", supplement="", chatid="c1", userid="u1"
    )
    assert slug is None and err and "不是 chat" in err


async def test_reject_chat_still_chatting(db, tmp_path, reply):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(db, cfg, reply)
    await _insert_chat_row(db, slug="chat-run", state=SessionState.CHATTING)
    slug, _repo, _ahead, err = await mgr.new_pipeline_from_chat(
        chat_slug="chat-run", supplement="", chatid="c1", userid="u1"
    )
    assert slug is None and err and "正在生成回复" in err


async def test_reject_bare_chat_no_repo(db, tmp_path, reply):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(db, cfg, reply)
    await _insert_chat_row(db, slug="chat-bare", repo=_NO_REPO)
    slug, _repo, _ahead, err = await mgr.new_pipeline_from_chat(
        chat_slug="chat-bare", supplement="", chatid="c1", userid="u1"
    )
    assert slug is None and err and "没有绑定仓库" in err


async def test_reject_repo_removed_from_config(db, tmp_path, reply):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(db, cfg, reply)
    await _insert_chat_row(db, slug="chat-ghost", repo="ghost-repo")
    slug, _repo, _ahead, err = await mgr.new_pipeline_from_chat(
        chat_slug="chat-ghost", supplement="", chatid="c1", userid="u1"
    )
    assert slug is None and err and "不在当前配置" in err


async def test_reject_csid_not_yet_persisted(db, tmp_path, reply):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(db, cfg, reply)
    await _insert_chat_row(db, slug="chat-nocsid", csid=None)
    slug, _repo, _ahead, err = await mgr.new_pipeline_from_chat(
        chat_slug="chat-nocsid", supplement="", chatid="c1", userid="u1"
    )
    assert slug is None and err and "还没成功回复过" in err


async def test_reject_double_dev(db, tmp_path, reply):
    """已有一条 pipeline row 以该 chat 为 origin → 二次 /dev 被 session_exists_with_origin 挡下。"""
    cfg = _cfg(tmp_path)
    mgr = SessionManager(db, cfg, reply)
    await _insert_chat_row(db, slug="chat-twice")
    await db.insert_session(
        {
            "slug": "req-already",
            "display_slug": "req-already",
            "repo": "repo-a",
            "state": "planning",
            "default_branch": "main",
            "initial_request": "x",
            "chatid": "c1",
            "userid": "u1",
            "origin_chat_slug": "chat-twice",
        }
    )
    slug, _repo, _ahead, err = await mgr.new_pipeline_from_chat(
        chat_slug="chat-twice", supplement="", chatid="c1", userid="u1"
    )
    assert slug is None and err and "已经转过开发任务" in err


# ---------- 端到端：经 App._on_message ----------


async def test_end_to_end_handoff_via_app(db, tmp_path, monkeypatch):
    """@a /chat → 引用回复带 tag → /dev 补充 → pipeline 跑到 COMPLETED；原 chat 归档。"""
    from cc_fleet.app import App
    from cc_fleet.bot.base import BotRunner
    from cc_fleet.bot.message import IncomingMessage

    cfg = _cfg(tmp_path)
    _patch_chat_runner(monkeypatch, text="讨论清楚啦")

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

    # 1) 开 chat
    await app._on_message(
        IncomingMessage(text="@a /chat 想加个 X 功能", quote_text="", chatid="c1", userid="u1")
    )
    chat_display = extract_quote_context(replies[0][1]).slug
    chat_internal = (await db.get_session_by_display_slug(chat_display))["slug"]
    await _wait_state(db, chat_internal, SessionState.CHAT_AWAITING)

    # 2) 引用 chat 输出（带 tag）+ /dev 转开发
    _patch_pipeline_runner(monkeypatch)
    tag_msg = [t for (_c, t) in replies if "[session:" in t][-1]
    await app._on_message(
        IncomingMessage(
            text="/dev 记得补单测", quote_text=tag_msg, chatid="c1", userid="u1"
        )
    )
    # handoff ack 带 pipeline 的 internal slug tag（req-… @repo-a）
    handoff_ack = [t for (_c, t) in replies if "转为开发任务" in t][-1]
    pctx = extract_quote_context(handoff_ack)
    assert pctx.slug and pctx.slug.startswith("req-")
    assert pctx.repo == "repo-a"

    await _wait_state(db, pctx.slug, SessionState.COMPLETED)
    prow = await db.get_session(pctx.slug)
    assert prow["origin_chat_slug"] == chat_internal
    # 原 chat 归档
    await _wait_state(db, chat_internal, SessionState.CANCELLED)
    await app._manager.shutdown()
