"""SessionManager 并发模型测试。

不走真实 claude / git，只测：
- 多个 session 并发能跑（不再被全局锁串行化）
- semaphore 上限触达时新 session 入队、自己前面有几个 in-flight 体现在 ahead 返回值上
- per-repo fetch_lock 真的被同 repo session 串行获取
- shutdown 能 drain 在飞 task 而不抛
- cancel 能让 awaiting 中的后台 task 干净退出
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import pytest

from cc_fleet.config.schema import (
    AppConfig,
    ClaudeConfig,
    LimitsConfig,
    PipelineConfig,
    RepoConfig,
    WecomConfig,
)
from cc_fleet.core import mr as mr_module
from cc_fleet.core import repo as repo_module
from cc_fleet.core.runners.base import ClaudeRunResult
from cc_fleet.core.session_manager import SessionManager
from cc_fleet.core.state import SessionState
from cc_fleet.storage.db import Database

from tests.conftest import FakeRunner, perm_mode


async def _wait_idle(mgr: SessionManager, timeout: float = 5.0) -> None:
    """等所有后台 task 自然跑到终态（_sessions 在 _session_loop 的 finally 里清空）。"""
    for _ in range(int(timeout / 0.01)):
        if not mgr._sessions:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等 task 完成超时，剩余 {list(mgr._sessions)}")


async def _wait_state(db: Database, slug: str, target: SessionState, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        row = await db.get_session(slug)
        if row and row["state"] == target.value:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"session {slug} 未在 {timeout}s 内进入 {target}")


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.db")
    await d.connect()
    yield d
    await d.close()


def _make_cfg(tmp_path: Path, *, max_concurrent: int = 4) -> AppConfig:
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        pipeline=PipelineConfig(plan_timeout_sec=10, dev_timeout_sec=10, max_clarify_rounds=2),
        repos=[
            RepoConfig(name="repo-a", path=repo_a, default_branch="main"),
            RepoConfig(name="repo-b", path=repo_b, default_branch="main"),
        ],
        limits=LimitsConfig(max_concurrent_sessions=max_concurrent),
    )


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return _make_cfg(tmp_path, max_concurrent=4)


@pytest.fixture
def replies() -> list:
    return []


@pytest.fixture
def reply(replies: list):
    async def _reply(chatid: str, text: str) -> None:
        replies.append((chatid, text))
    return _reply


@pytest.fixture(autouse=True)
def stub_repo_and_mr(monkeypatch: pytest.MonkeyPatch):
    """整个文件统一 stub git / mr：让 Session._do_new 跑通而不需要真仓库。"""
    fetch_calls: list[str] = []

    async def fake_fetch(repo_root: Path, _branch: str, _remote: str = "origin") -> None:
        fetch_calls.append(str(repo_root))

    async def fake_create_worktree(_root: Path, path: Path, _branch: str, _base: str) -> None:
        path.mkdir(parents=True, exist_ok=True)

    async def fake_has_commits_ahead(_path: Path, _base: str) -> bool:
        return True

    async def fake_mr_create(**_kwargs) -> str:
        return "https://gitlab/example/-/merge_requests/1"

    monkeypatch.setattr(repo_module, "fetch_default_branch", fake_fetch)
    monkeypatch.setattr(repo_module, "create_worktree", fake_create_worktree)
    monkeypatch.setattr(repo_module, "has_commits_ahead", fake_has_commits_ahead)
    monkeypatch.setattr(mr_module, "create_mr_via_push", fake_mr_create)
    # 把 fetch_calls 挂在 pytest 的 conftest-like 容器上，让需要的 test 取出来检查
    monkeypatch.setitem(globals(), "_FETCH_CALLS", fetch_calls)


def _patch_claude_smart(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan_text: str = "plan ready\n\nSLUG: feat\nSTATUS: READY",
    dev_text: str = "完成 ✅",
) -> None:
    """按 permission_mode 路由的 claude stub，并发场景下不会因为全局 call_count 串台。

    plan_text 应当含 ``SLUG: <slug>``——多 session 并发时同 slug 会被 resolve_slug_conflict
    自动加后缀（feat / feat-2 / ...），不需要每条 session 各起一个。
    """
    from cc_fleet.core import session as session_mod

    async def stub(**kwargs) -> ClaudeRunResult:
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


def _patch_claude_clarification(
    monkeypatch: pytest.MonkeyPatch,
    *,
    slug: str = "add-login",
    question: str = "密码还是 OAuth？",
) -> None:
    """先 NEED_CLARIFICATION 一轮，第二次 plan 后 READY，最后 dev 完成。

    用 session_id 维度的 first-call 标记：每个 claude_session_id 第一次进 plan 返
    NEED_CLARIFICATION，之后回 READY。这样多 session 并发也不会串台。
    """
    from cc_fleet.core import session as session_mod

    seen_plan: set[str] = set()

    async def stub(**kwargs) -> ClaudeRunResult:
        mode = perm_mode(kwargs)
        sid = kwargs.get("resume_from") or kwargs["session_id"]
        if mode == "plan":
            if sid not in seen_plan:
                seen_plan.add(sid)
                text = f"SLUG: {slug}\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n- {question}"
            else:
                text = f"SLUG: {slug}\nSTATUS: READY"
        else:
            text = "完成 ✅"
        return ClaudeRunResult(
            exit_code=0,
            session_id=sid,
            text_output=text,
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))


def _patch_claude_dev_clarification(
    monkeypatch: pytest.MonkeyPatch,
    *,
    slug: str = "add-login",
    question: str = "A 还是 B？",
) -> None:
    """plan 直接 READY；dev 首轮 NEED_CLARIFICATION、之后 READY 完成。

    以 claude_session_id 维度标记 dev 首轮，多 session 并发不串台。
    """
    from cc_fleet.core import session as session_mod

    seen_dev: set[str] = set()

    async def stub(**kwargs) -> ClaudeRunResult:
        mode = perm_mode(kwargs)
        sid = kwargs.get("resume_from") or kwargs["session_id"]
        if mode == "plan":
            text = f"SLUG: {slug}\nSTATUS: READY"
        elif sid not in seen_dev:
            seen_dev.add(sid)
            text = f"STATUS: NEED_CLARIFICATION\nQUESTIONS:\n1. {question}"
        else:
            text = "完成 ✅"
        return ClaudeRunResult(
            exit_code=0,
            session_id=sid,
            text_output=text,
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))


# ---------- happy path：两个 session 并发跑到 COMPLETED ----------

async def test_two_sessions_run_in_parallel(db, cfg, reply, monkeypatch):
    """两个不同 repo 的 session 同时 new_session，应当**并发**跑到 COMPLETED，
    而不是被全局锁串行化。"""
    _patch_claude_smart(monkeypatch, plan_text="plan ready\n\nSLUG: feat\nSTATUS: READY")
    mgr = SessionManager(db, cfg, reply)

    slug_a, ahead_a = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="x", chatid="c1", userid="u1"
    )
    slug_b, ahead_b = await mgr.new_session(
        repo_cfg=cfg.repos[1], text="y", chatid="c1", userid="u1"
    )
    # max_concurrent=4，两条都不该排队
    assert ahead_a == 0
    assert ahead_b == 1  # ahead 是"自己前面有几个 in-flight"，第二个看到第一个

    # 等两个后台 task 自然跑完
    await _wait_idle(mgr)
    await mgr.shutdown()

    row_a = await db.get_session(slug_a)
    row_b = await db.get_session(slug_b)
    assert row_a["state"] == SessionState.COMPLETED.value
    assert row_b["state"] == SessionState.COMPLETED.value


# ---------- semaphore 上限触达 → 自动排队 ----------

async def test_third_session_queues_when_slots_full(tmp_path, db, reply, monkeypatch):
    """max_concurrent_sessions=1：第二个 session new_session 时 ahead 应 ==1（前面有一个在跑）。"""
    cfg = _make_cfg(tmp_path, max_concurrent=1)
    _patch_claude_smart(monkeypatch, plan_text="SLUG: x-y\nSTATUS: READY")
    mgr = SessionManager(db, cfg, reply)

    # 起第一个：ahead==0
    slug1, ahead1 = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="r1", chatid="c1", userid="u1"
    )
    assert ahead1 == 0

    # 第二个：前面有 1 个 in-flight
    slug2, ahead2 = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="r2", chatid="c1", userid="u1"
    )
    assert ahead2 == 1
    assert slug1 != slug2

    await mgr.shutdown()


# ---------- per-repo fetch_lock 真的串行同 repo 的 fetch ----------

async def test_same_repo_fetch_serialized(tmp_path, db, reply, monkeypatch):
    """同一 repo 两个 session 并发，fetch_default_branch 必须在 per-repo lock 下串行调用。

    通过把 fetch_lock 套上一个"声明 + 校验"装饰：进入时计数+1（要求 ==1），退出时 -1。
    任何时刻只允许一个 session 持有该 repo 的 fetch。
    """
    cfg = _make_cfg(tmp_path, max_concurrent=4)

    in_flight = {"n": 0}
    violations: list[str] = []
    orig_fetch = repo_module.fetch_default_branch

    async def watching_fetch(
        repo_root: Path, branch: str, remote: str = "origin"
    ) -> None:
        in_flight["n"] += 1
        if in_flight["n"] > 1:
            violations.append(f"concurrent fetch on {repo_root}")
        await asyncio.sleep(0.01)  # 给并发一个真窗口
        in_flight["n"] -= 1
        await orig_fetch(repo_root, branch, remote)  # 调 monkeypatched 那个空实现

    monkeypatch.setattr(repo_module, "fetch_default_branch", watching_fetch)
    _patch_claude_smart(monkeypatch, plan_text="SLUG: same-repo\nSTATUS: READY")

    mgr = SessionManager(db, cfg, reply)
    await mgr.new_session(repo_cfg=cfg.repos[0], text="r1", chatid="c1", userid="u1")
    await mgr.new_session(repo_cfg=cfg.repos[0], text="r2", chatid="c1", userid="u1")
    await _wait_idle(mgr)
    await mgr.shutdown()

    assert violations == [], f"per-repo fetch 串行被违反：{violations}"


# ---------- continue_session：awaiting 中的 session 通过 resume_event 唤醒 ----------

async def test_continue_session_resumes_awaiting(db, cfg, reply, replies, monkeypatch):
    """新 session 进 awaiting → continue_session 不重新排队（不 acquire semaphore）"""
    _patch_claude_clarification(monkeypatch, slug="add-login")
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="加登录", chatid="c1", userid="u1"
    )

    # 轮询等 session 进 awaiting（plan 跑完）
    for _ in range(100):
        row = await db.get_session(slug)
        if row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value:
            break
        await asyncio.sleep(0.01)
    else:
        await mgr.shutdown()
        pytest.fail("session 未进入 awaiting")

    # 这时 ctx 应该在 _sessions 里
    assert slug in mgr._sessions

    # 用 display_slug 喂回复（continue_session 既认 display 也认 internal slug）
    ok = await mgr.continue_session(
        slug="add-login", text="用密码", quote_text=None
    )
    assert ok is True

    await _wait_idle(mgr)
    await mgr.shutdown()
    row = await db.get_session(slug)
    assert row["state"] == SessionState.COMPLETED.value


async def test_continue_session_accepts_internal_slug(db, cfg, reply, monkeypatch):
    """初始 ack 期间 display_slug 尚未生成，ack tag 里挂的是 internal slug。用户引用
    该消息追加文字时，``continue_session`` 必须能用 internal slug 命中同一 session
    （否则会被上层判作"未找到未结案 session"，回退兜底）。"""
    _patch_claude_clarification(monkeypatch, slug="add-login")
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="加登录", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.AWAITING_USER_CLARIFICATION)

    # 关键：用 internal slug（new_session 返回的 slug，形如 req-...）而非 display_slug
    ok = await mgr.continue_session(slug=slug, text="用密码", quote_text=None)
    assert ok is True

    await _wait_idle(mgr)
    await mgr.shutdown()
    row = await db.get_session(slug)
    assert row["state"] == SessionState.COMPLETED.value


# ---------- continue_session ack：澄清回复成功后立即回包给用户 ----------

async def test_continue_session_awaiting_acks_user(db, cfg, reply, replies, monkeypatch):
    """awaiting → continue_session 成功后应立即向用户回包一句 ack（"已收到补充信息"），
    避免用户引用回复后看不到任何反馈。"""
    _patch_claude_clarification(monkeypatch, slug="add-login")
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="加登录", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.AWAITING_USER_CLARIFICATION)

    before = len(replies)
    ok = await mgr.continue_session(
        slug="add-login", text="用密码", quote_text=None
    )
    assert ok is True

    # ack 在 set resume_event 前同步发出，调用 continue_session 返回时应已入列
    new_replies = replies[before:]
    ack_texts = [text for chatid, text in new_replies if chatid == "c1"]
    assert any(
        "已收到补充信息" in t and "add-login" in t and "[session: add-login" in t
        for t in ack_texts
    ), f"未在 ack 中找到澄清文案：{ack_texts}"

    await _wait_idle(mgr)
    await mgr.shutdown()


# ---------- continue_session：dev 阶段 awaiting 唤醒回 developing ----------

async def test_continue_session_resumes_dev_awaiting(db, cfg, reply, replies, monkeypatch):
    """dev 阶段 NEED_CLARIFICATION → awaiting → continue_session 唤醒回 developing → COMPLETED，
    且全程复用同一 ctx（不重新排队）。"""
    _patch_claude_dev_clarification(monkeypatch, slug="add-login")
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="加登录", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.AWAITING_USER_CLARIFICATION)
    row = await db.get_session(slug)
    assert row["clarify_phase"] == "developing"
    assert slug in mgr._sessions  # awaiting 占着 ctx，未重新排队

    ok = await mgr.continue_session(slug="add-login", text="选 A", quote_text=None)
    assert ok is True

    await _wait_idle(mgr)
    await mgr.shutdown()
    row = await db.get_session(slug)
    assert row["state"] == SessionState.COMPLETED.value


async def test_continue_session_dev_awaiting_ack_says_kaifa(db, cfg, reply, replies, monkeypatch):
    """dev 澄清唤醒的 ack 文案应说「继续推进开发」而非「plan」。"""
    _patch_claude_dev_clarification(monkeypatch, slug="add-login")
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="加登录", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.AWAITING_USER_CLARIFICATION)

    before = len(replies)
    ok = await mgr.continue_session(slug="add-login", text="选 A", quote_text=None)
    assert ok is True
    ack_texts = [t for c, t in replies[before:] if c == "c1"]
    assert any("已收到补充信息" in t and "继续推进开发" in t for t in ack_texts), ack_texts

    await _wait_idle(mgr)
    await mgr.shutdown()


# ---------- continue_session ack：复活 failed/timeout/completed 后立即回包 ----------

async def test_continue_session_revive_acks_user(db, cfg, reply, replies, monkeypatch):
    """FAILED session 被引用回复唤醒时，复活路径应立即回包一句 "已收到回复"。
    本测试 max_concurrent=4，复活时无 in-flight，ack 不含队列前缀。"""
    from cc_fleet.core import session as session_mod

    call_count = {"n": 0}

    async def stub(**kwargs) -> ClaudeRunResult:
        mode = perm_mode(kwargs)
        if mode == "plan":
            return ClaudeRunResult(
                exit_code=0,
                session_id=kwargs.get("resume_from") or kwargs["session_id"],
                text_output="SLUG: fix-bug\nSTATUS: READY",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="",
                timed_out=False,
            )
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ClaudeRunResult(
                exit_code=1,
                session_id=kwargs.get("resume_from") or kwargs["session_id"],
                text_output="",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="initial dev failed",
                timed_out=False,
            )
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="二次开发完成 ✅",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))

    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="修个 bug", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.FAILED)
    await _wait_idle(mgr)

    row = await db.get_session(slug)
    display = row["display_slug"]
    assert display is not None

    before = len(replies)
    ok = await mgr.continue_session(
        slug=display, text="commit 漏了 build,重试", quote_text=None
    )
    assert ok is True

    new_replies = replies[before:]
    ack_texts = [text for chatid, text in new_replies if chatid == "c1"]
    # 复活时 _pending==0,ack 应为 "已收到回复,claude 正在继续推进 [display]"
    assert any(
        "已收到回复" in t and display in t and "[session:" in t and "前面" not in t
        for t in ack_texts
    ), f"未在 ack 中找到复活文案：{ack_texts}"

    await _wait_state(db, slug, SessionState.COMPLETED)
    await _wait_idle(mgr)
    await mgr.shutdown()


# ---------- continue_session ack：复活时有在跑 session，ack 应携带排队前缀 ----------

async def test_continue_session_revive_ack_includes_queue_position(
    tmp_path, db, reply, replies, monkeypatch
):
    """max_concurrent=1：先起一个 awaiting session 占着 semaphore，再让另一个 session
    跑到 FAILED 后被引用回复唤醒。此时 _pending==1，ack 文案应含 "前面 1 个"。"""
    from cc_fleet.core import session as session_mod

    cfg = _make_cfg(tmp_path, max_concurrent=1)

    # session A：稳定卡在 awaiting；session B：plan 完成后 dev 失败（首轮）→ 复活时成功
    call_count = {"dev": 0}

    async def stub(**kwargs) -> ClaudeRunResult:
        mode = perm_mode(kwargs)
        sid = kwargs.get("resume_from") or kwargs["session_id"]
        # 通过 cwd 路径区分 session（不同 session 在不同 sessions/<slug> 下）
        if mode == "plan":
            # B（fix-bug）的 plan 直接 READY；A 用另一个 slug
            return ClaudeRunResult(
                exit_code=0,
                session_id=sid,
                text_output="SLUG: fix-bug\nSTATUS: READY",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="",
                timed_out=False,
            )
        call_count["dev"] += 1
        if call_count["dev"] == 1:
            return ClaudeRunResult(
                exit_code=1,
                session_id=sid,
                text_output="",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="initial dev failed",
                timed_out=False,
            )
        return ClaudeRunResult(
            exit_code=0,
            session_id=sid,
            text_output="二次开发完成 ✅",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))

    mgr = SessionManager(db, cfg, reply)
    # session B：跑到 FAILED
    slug_b, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="修个 bug", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug_b, SessionState.FAILED)
    await _wait_idle(mgr)
    row_b = await db.get_session(slug_b)
    display_b = row_b["display_slug"]

    # session A：占着 semaphore，让 B 复活时排队
    async def stub_a(**kwargs) -> ClaudeRunResult:
        await asyncio.sleep(5)  # 模拟长时间不返回，保证 _pending 在 B 复活时为 1
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="SLUG: hold\nSTATUS: READY",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub_a))
    slug_a, ahead_a = await mgr.new_session(
        repo_cfg=cfg.repos[1], text="占槽", chatid="c1", userid="u1"
    )
    assert ahead_a == 0

    # 立刻复活 B，此时 _pending==1（A 还没跑完）
    before = len(replies)
    ok = await mgr.continue_session(
        slug=display_b, text="重试", quote_text=None
    )
    assert ok is True

    new_replies = replies[before:]
    ack_texts = [text for chatid, text in new_replies if chatid == "c1"]
    assert any(
        "已收到回复" in t and "前面 1 个" in t and display_b in t
        for t in ack_texts
    ), f"未在 ack 中找到排队文案：{ack_texts}"

    # 清场：取消 A、清退 B（无需等到完成）
    await mgr.cancel(slug_a)
    await mgr.cancel(slug_b)
    await mgr.shutdown()


# ---------- continue_session ack：复活时 in-flight 未触达上限，ack 不应回排队前缀 ----------


async def test_continue_session_revive_ack_no_queue_when_below_limit(
    tmp_path, db, reply, replies, monkeypatch
):
    """max_concurrent=4：让一个 session 在 dev 阶段长跑占着 _pending，让另一个
    session 跑到 FAILED 后被引用回复唤醒。此时 _pending==1 < max=4，新 task 立刻能
    拿到槽位开跑，ack 不应回"前面 N 个"前缀。

    回归保护：早期串行模型下 ``ahead > 0`` 判断在并发模式下会误把"已有任一
    in-flight"当成排队，文案与 ``_session_loop`` 真实调度脱节。"""
    from cc_fleet.core import session as session_mod

    cfg = _make_cfg(tmp_path, max_concurrent=4)

    call_count = {"dev": 0}

    async def stub(**kwargs) -> ClaudeRunResult:
        mode = perm_mode(kwargs)
        sid = kwargs.get("resume_from") or kwargs["session_id"]
        if mode == "plan":
            return ClaudeRunResult(
                exit_code=0,
                session_id=sid,
                text_output="SLUG: fix-bug\nSTATUS: READY",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="",
                timed_out=False,
            )
        call_count["dev"] += 1
        if call_count["dev"] == 1:
            return ClaudeRunResult(
                exit_code=1,
                session_id=sid,
                text_output="",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="initial dev failed",
                timed_out=False,
            )
        return ClaudeRunResult(
            exit_code=0,
            session_id=sid,
            text_output="二次开发完成 ✅",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))

    mgr = SessionManager(db, cfg, reply)
    # session B：跑到 FAILED
    slug_b, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="修个 bug", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug_b, SessionState.FAILED)
    await _wait_idle(mgr)
    row_b = await db.get_session(slug_b)
    display_b = row_b["display_slug"]

    # session A：dev 阶段长跑不返回，占着 _pending（plan 阶段照常完成，进 dev 后卡住）
    async def stub_a(**kwargs) -> ClaudeRunResult:
        mode = perm_mode(kwargs)
        sid = kwargs.get("resume_from") or kwargs["session_id"]
        if mode == "plan":
            return ClaudeRunResult(
                exit_code=0,
                session_id=sid,
                text_output="SLUG: hold\nSTATUS: READY",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="",
                timed_out=False,
            )
        await asyncio.sleep(5)  # dev 阶段长跑，保证 B 复活时 _pending==1
        return ClaudeRunResult(
            exit_code=0,
            session_id=sid,
            text_output="done",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub_a))
    slug_a, ahead_a = await mgr.new_session(
        repo_cfg=cfg.repos[1], text="占位", chatid="c1", userid="u1"
    )
    assert ahead_a == 0

    # 等 A 真正占住 _pending，否则可能在 continue_session 触发时 A 的 _session_loop 还没起来
    for _ in range(200):
        if mgr._pending >= 1:
            break
        await asyncio.sleep(0.01)
    assert mgr._pending == 1

    # 复活 B，此时 _pending==1 < max=4，新 task 立刻拿到槽位，ack 不应回排队前缀
    before = len(replies)
    ok = await mgr.continue_session(
        slug=display_b, text="重试", quote_text=None
    )
    assert ok is True

    new_replies = replies[before:]
    ack_texts = [text for chatid, text in new_replies if chatid == "c1"]
    assert any(
        "已收到回复" in t and display_b in t and "前面" not in t
        for t in ack_texts
    ), f"未在 ack 中找到非排队复活文案：{ack_texts}"

    # 清场：取消 A、等 B 复活到 COMPLETED 也无所谓，直接 shutdown 走 cancel 路径
    await mgr.cancel(slug_a)
    await mgr.cancel(slug_b)
    await mgr.shutdown()


# ---------- cancel：能取消 in-flight 的 awaiting session ----------

async def test_cancel_awaiting_session(db, cfg, reply, monkeypatch):
    # 让 stub 永远只回 NEED_CLARIFICATION，确保 session 卡在 awaiting 让 cancel 触发 resume_event
    from cc_fleet.core import session as session_mod

    async def stub(**kwargs) -> ClaudeRunResult:
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="SLUG: add-x\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n- A",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="x", chatid="c1", userid="u1"
    )
    for _ in range(100):
        row = await db.get_session(slug)
        if row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value:
            break
        await asyncio.sleep(0.01)
    else:
        await mgr.shutdown()
        pytest.fail("session 未进入 awaiting")

    ok = await mgr.cancel("add-x")  # 用 display_slug 取消
    assert ok is True
    await mgr.shutdown()

    row = await db.get_session(slug)
    assert row["state"] == SessionState.CANCELLED.value


async def test_cancel_unknown_slug_returns_false(db, cfg, reply):
    mgr = SessionManager(db, cfg, reply)
    ok = await mgr.cancel("no-such-slug")
    assert ok is False
    await mgr.shutdown()


async def test_hard_cancel_sets_kill_event_and_cancels(db, cfg, reply, monkeypatch):
    """/kill：hard_cancel 对内存中的 session set kill_event（供 engine 立即杀活进程）
    并复用 cancel() 落 CANCELLED。用卡在 awaiting 的 session 观察 kill_event 被按下。"""
    from cc_fleet.core import session as session_mod

    async def stub(**kwargs) -> ClaudeRunResult:
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="SLUG: kill-x\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n- A",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="x", chatid="c1", userid="u1"
    )
    for _ in range(100):
        row = await db.get_session(slug)
        if row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value:
            break
        await asyncio.sleep(0.01)
    else:
        await mgr.shutdown()
        pytest.fail("session 未进入 awaiting")

    ctx = mgr._sessions[slug]  # 内存 ctx（hard_cancel 应 set 其 session 的 kill_event）
    ok = await mgr.hard_cancel("kill-x")  # 用 display_slug 强杀
    assert ok is True
    assert ctx.session._kill_event.is_set()
    await mgr.shutdown()

    row = await db.get_session(slug)
    assert row["state"] == SessionState.CANCELLED.value


async def test_hard_cancel_unknown_slug_returns_false(db, cfg, reply):
    mgr = SessionManager(db, cfg, reply)
    ok = await mgr.hard_cancel("no-such-slug")
    assert ok is False
    await mgr.shutdown()


# ---------- shutdown drain ----------

# ---------- continue_session：失败/超时/已完成 session 的复活流 ----------


async def test_continue_session_revives_failed_session(db, cfg, reply, replies, monkeypatch):
    """FAILED session（旧后台 task 已退、_sessions 里没 ctx）被引用回复唤醒：
    apply_followup 切回 DEVELOPING，SessionManager 起新 task 接着 drive 到 COMPLETED。"""
    from cc_fleet.core import session as session_mod

    # 先让 dev 失败一次，再让二次 dev 成功
    call_count = {"n": 0}

    async def stub(**kwargs) -> ClaudeRunResult:
        mode = perm_mode(kwargs)
        if mode == "plan":
            return ClaudeRunResult(
                exit_code=0,
                session_id=kwargs.get("resume_from") or kwargs["session_id"],
                text_output="SLUG: fix-bug\nSTATUS: READY",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="",
                timed_out=False,
            )
        # dev 模式
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ClaudeRunResult(
                exit_code=1,
                session_id=kwargs.get("resume_from") or kwargs["session_id"],
                text_output="",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="initial dev failed",
                timed_out=False,
            )
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="二次开发完成 ✅",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))

    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="修个 bug", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.FAILED)
    await _wait_idle(mgr)  # 旧 task 已退、ctx 已清

    row = await db.get_session(slug)
    display = row["display_slug"]
    assert display is not None
    assert slug not in mgr._sessions  # ctx 已清

    # 用户引用失败回执回复
    ok = await mgr.continue_session(
        slug=display, text="commit 漏了 build，重试", quote_text=None
    )
    assert ok is True
    assert slug in mgr._sessions  # 新 task 已起

    # 复活 task 跑到 COMPLETED
    await _wait_state(db, slug, SessionState.COMPLETED)
    await _wait_idle(mgr)
    await mgr.shutdown()


async def test_continue_session_cancelled_returns_false(db, cfg, reply, monkeypatch):
    """CANCELLED session 不属于 is_open → continue_session 直接返回 False，
    上层 dispatcher 已经把这种 quote 路由成 NEW，这里是防御。"""
    from cc_fleet.core import session as session_mod

    async def stub(**kwargs) -> ClaudeRunResult:
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="SLUG: dead-x\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n- A",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(stub))
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="x", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.AWAITING_USER_CLARIFICATION)
    ok = await mgr.cancel("dead-x")
    assert ok is True

    ok = await mgr.continue_session(
        slug="dead-x", text="继续", quote_text=None
    )
    assert ok is False
    await mgr.shutdown()


async def test_shutdown_drains_inflight_tasks(db, cfg, reply, monkeypatch):
    """shutdown 应能干净地取消并等待所有 in-flight task，无残留 warning。"""
    from cc_fleet.core import session as session_mod

    async def slow_stub(**kwargs) -> ClaudeRunResult:
        await asyncio.sleep(0.2)
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="SLUG: long\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n- ?",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(slow_stub))
    mgr = SessionManager(db, cfg, reply)
    for i in range(3):
        await mgr.new_session(
            repo_cfg=cfg.repos[0], text=f"r{i}", chatid="c1", userid="u1"
        )
    # 给它们一点时间走起来
    await asyncio.sleep(0.05)
    await mgr.shutdown()
    # 所有 ctx 应该清干净
    assert mgr._sessions == {}


# ---------- drive 内抛未捕获异常 → 兜底转 FAILED ----------

async def test_drive_unhandled_exception_marks_failed(db, cfg, reply, replies, monkeypatch):
    """模拟 _do_planning 阶段 run_claude 抛 ValueError（之前的 64KB readline 触发链路）：
    session_manager 顶层兜底必须把 session 转 FAILED 并通知用户，而不是悬挂在 planning。"""
    from cc_fleet.core import session as session_mod

    async def boom(**kwargs) -> ClaudeRunResult:
        raise ValueError("Separator is found, but chunk is longer than limit")

    monkeypatch.setattr(session_mod, "get_runner", lambda *a, **k: FakeRunner(boom))

    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="触发异常", chatid="c1", userid="u1"
    )

    await _wait_state(db, slug, SessionState.FAILED)
    await _wait_idle(mgr)
    await mgr.shutdown()

    row = await db.get_session(slug)
    assert row["state"] == SessionState.FAILED.value
    assert row["failed_phase"] == SessionState.PLANNING.value
    assert "主控异常未捕获" in (row["last_error"] or "")
    assert "ValueError" in (row["last_error"] or "")
    # 用户应收到失败通知
    fail_notices = [t for chatid, t in replies if "❌" in t and "失败" in t]
    assert fail_notices, f"未发出失败通知：{replies}"


async def test_drive_exception_does_not_overwrite_cancelled(db, cfg, reply, monkeypatch):
    """先 cancel 一个 awaiting session，再人为让其 ctx 上抛异常：兜底逻辑不应把已经
    CANCELLED 的 session 覆盖回 FAILED。验证 is_terminal 早返回分支。"""
    _patch_claude_clarification(monkeypatch, slug="cancel-then-boom")
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="先 cancel", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.AWAITING_USER_CLARIFICATION)

    ctx = mgr._sessions[slug]
    # 取消 → CANCELLED；ctx 还在 _sessions 里直到后台 task 收到 cancel_requested 退出
    ok = await mgr.cancel("cancel-then-boom")
    assert ok is True
    await _wait_state(db, slug, SessionState.CANCELLED)
    await _wait_idle(mgr)

    # 显式调一次兜底：state 已是 terminal 应直接返回，不动 DB
    before_row = await db.get_session(slug)
    await mgr._mark_failed_on_drive_exception(ctx, RuntimeError("late boom"))
    after_row = await db.get_session(slug)
    assert after_row["state"] == SessionState.CANCELLED.value
    assert after_row["state"] == before_row["state"]
    await mgr.shutdown()


# ---------- resume_session：显式 /resume 拉起 working 状态孤儿 session ----------


def _make_orphan_row(
    *,
    slug: str,
    repo: str,
    state: SessionState,
    worktree_path: str | None = None,
    display_slug: str | None = None,
    claude_session_id: str | None = None,
    branch: str | None = None,
    default_branch: str = "main",
) -> dict:
    return {
        "slug": slug,
        "display_slug": display_slug,
        "repo": repo,
        "state": state.value,
        "claude_session_id": claude_session_id,
        "worktree_path": worktree_path,
        "branch": branch,
        "default_branch": default_branch,
        "initial_request": "test request",
        "chatid": "c1",
        "userid": "u1",
        "clarify_rounds": 0,
        "last_error": None,
        "mr_url": None,
    }


async def test_resume_developing_session(tmp_path, db, cfg, reply, monkeypatch):
    """state=DEVELOPING 的孤儿 row + worktree 健在 → /resume 起后台 task,跑到 COMPLETED。
    覆盖核心修复场景：主控被 kill 留下的 developing,用户显式 /resume 拉起。"""
    _patch_claude_smart(monkeypatch)
    wt = tmp_path / "ws" / "sessions" / "req-old-dev" / "worktree"
    wt.mkdir(parents=True)
    await db.insert_session(_make_orphan_row(
        slug="req-old-dev",
        repo=cfg.repos[0].name,
        state=SessionState.DEVELOPING,
        worktree_path=str(wt),
        display_slug="resume-this",
        claude_session_id="sid-old-1",
        branch="claude/req-old-dev",
    ))

    mgr = SessionManager(db, cfg, reply)
    ok, ack = await mgr.resume_session("resume-this")
    assert ok is True
    assert "resume-this" in ack
    assert "已恢复推进" in ack
    assert "req-old-dev" in mgr._sessions

    await _wait_state(db, "req-old-dev", SessionState.COMPLETED)
    await _wait_idle(mgr)
    await mgr.shutdown()


async def test_resume_new_with_existing_worktree_is_idempotent(
    tmp_path, db, cfg, reply, monkeypatch
):
    """state=NEW 孤儿 + 已存在 worktree（含 .git）→ /resume 时 _do_new 跳过 fetch + create,
    且保留已分配的 claude_session_id。回归保护:无条件 new_uuid 会丢 SDK 会话。"""
    _patch_claude_smart(monkeypatch)

    # worktree 路径必须与 _do_new 的推导一致（<repo.path>-worktrees/<slug>），
    # 否则 already_exists 判 False，会误走 fetch+create 且 row 指向不存在的目录。
    wt = cfg.repos[0].path.with_name(cfg.repos[0].path.name + "-worktrees") / "req-resume-new"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /irrelevant\n")

    fetch_calls: list[str] = []
    create_calls: list[str] = []

    async def watching_fetch(
        repo_root: Path, _branch: str, _remote: str = "origin"
    ) -> None:
        fetch_calls.append(str(repo_root))

    async def watching_create(_root: Path, path: Path, _branch: str, _base: str) -> None:
        create_calls.append(str(path))

    monkeypatch.setattr(repo_module, "fetch_default_branch", watching_fetch)
    monkeypatch.setattr(repo_module, "create_worktree", watching_create)

    await db.insert_session(_make_orphan_row(
        slug="req-resume-new",
        repo=cfg.repos[0].name,
        state=SessionState.NEW,
        worktree_path=None,
        display_slug="resume-new",
        claude_session_id="sid-preserved",
    ))

    mgr = SessionManager(db, cfg, reply)
    ok, _ = await mgr.resume_session("req-resume-new")  # internal slug 也能用
    assert ok is True

    await _wait_state(db, "req-resume-new", SessionState.COMPLETED)
    await _wait_idle(mgr)
    await mgr.shutdown()

    assert fetch_calls == [], f"幂等失败,fetch 被调用：{fetch_calls}"
    assert create_calls == [], f"幂等失败,create_worktree 被调用：{create_calls}"

    row = await db.get_session("req-resume-new")
    assert row["claude_session_id"] == "sid-preserved"


async def test_resume_awaiting_rejected_with_hint(tmp_path, db, cfg, reply):
    """AWAITING 状态 → 拒绝,提示用户引用 plan 反问消息回答而不是 /resume。"""
    wt = tmp_path / "wt"
    wt.mkdir()
    await db.insert_session(_make_orphan_row(
        slug="req-aw",
        repo=cfg.repos[0].name,
        state=SessionState.AWAITING_USER_CLARIFICATION,
        worktree_path=str(wt),
        display_slug="aw-session",
    ))

    mgr = SessionManager(db, cfg, reply)
    ok, msg = await mgr.resume_session("aw-session")
    assert ok is False
    assert "引用" in msg and "澄清" in msg
    assert "req-aw" not in mgr._sessions
    await mgr.shutdown()


async def test_resume_terminal_states_rejected(tmp_path, db, cfg, reply):
    """COMPLETED / FAILED / TIMEOUT → 拒绝,引导引用回复唤醒;CANCELLED 拒绝建议重发。"""
    wt = tmp_path / "wt"
    wt.mkdir()
    cases = [
        ("req-done", SessionState.COMPLETED, "引用"),
        ("req-fail", SessionState.FAILED, "引用"),
        ("req-cancel", SessionState.CANCELLED, "取消"),
    ]
    for slug, st, _ in cases:
        await db.insert_session(_make_orphan_row(
            slug=slug,
            repo=cfg.repos[0].name,
            state=st,
            worktree_path=str(wt),
            display_slug=slug,
        ))

    mgr = SessionManager(db, cfg, reply)
    for slug, st, expected_hint in cases:
        ok, msg = await mgr.resume_session(slug)
        assert ok is False, f"{slug} ({st.value}) 不应被恢复"
        assert expected_hint in msg, f"{slug} 提示不含 {expected_hint!r}：{msg}"
        assert slug not in mgr._sessions
    await mgr.shutdown()


async def test_resume_already_in_memory_rejected(tmp_path, db, cfg, reply, monkeypatch):
    """已经在 _sessions 内存中的 session → /resume 拒绝,避免重叠 task。"""
    _patch_claude_clarification(monkeypatch, slug="busy-session")
    mgr = SessionManager(db, cfg, reply)
    slug, _ = await mgr.new_session(
        repo_cfg=cfg.repos[0], text="x", chatid="c1", userid="u1"
    )
    await _wait_state(db, slug, SessionState.AWAITING_USER_CLARIFICATION)
    assert slug in mgr._sessions

    ok, msg = await mgr.resume_session("busy-session")
    assert ok is False
    assert "内存" in msg
    await mgr.shutdown()


async def test_resume_unknown_slug_rejected(db, cfg, reply):
    """slug 在 db 完全不存在 → 拒绝。"""
    mgr = SessionManager(db, cfg, reply)
    ok, msg = await mgr.resume_session("no-such-slug")
    assert ok is False
    assert "未找到" in msg
    await mgr.shutdown()


async def test_resume_local_worktree_missing_rejected(tmp_path, db, cfg, reply):
    """local 模式 + DEVELOPING + worktree 目录不存在 → 拒绝并提示重发需求,不起 task。
    与原 recover 路径"标 FAILED"不同：/resume 是显式动作,失败不应静默改状态。"""
    await db.insert_session(_make_orphan_row(
        slug="req-no-wt",
        repo=cfg.repos[0].name,
        state=SessionState.DEVELOPING,
        worktree_path=str(tmp_path / "ghost-worktree"),
        display_slug="no-worktree",
    ))

    mgr = SessionManager(db, cfg, reply)
    ok, msg = await mgr.resume_session("no-worktree")
    assert ok is False
    assert "worktree" in msg and "丢失" in msg
    assert "req-no-wt" not in mgr._sessions

    # 状态不应被改（拒绝就是拒绝,不静默落 FAILED）
    row = await db.get_session("req-no-wt")
    assert row["state"] == SessionState.DEVELOPING.value
    await mgr.shutdown()


async def test_resume_unknown_repo_in_config_rejected(tmp_path, db, cfg, reply):
    """row.repo 不在 config → 拒绝,不改状态。"""
    wt = tmp_path / "wt"
    wt.mkdir()
    await db.insert_session(_make_orphan_row(
        slug="req-gone-repo",
        repo="nonexistent-repo",
        state=SessionState.DEVELOPING,
        worktree_path=str(wt),
        display_slug="gone",
    ))

    mgr = SessionManager(db, cfg, reply)
    ok, msg = await mgr.resume_session("gone")
    assert ok is False
    assert "nonexistent-repo" in msg

    row = await db.get_session("req-gone-repo")
    assert row["state"] == SessionState.DEVELOPING.value
    await mgr.shutdown()
