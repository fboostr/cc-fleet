"""Session 状态机端到端骨架测试（mock claude / repo / mr）。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from cc_fleet.config.schema import (
    AppConfig,
    ClaudeConfig,
    LimitsConfig,
    PipelineConfig,
    RepoConfig,
    ReviewerConfig,
    WecomConfig,
)
from cc_fleet.core import mr as mr_module
from cc_fleet.core import repo as repo_module
from cc_fleet.core.runners.base import AgentPermission, ClaudeRunResult
from cc_fleet.core.session import Session
from cc_fleet.core.state import SessionState
from cc_fleet.storage.db import Database

from tests.conftest import fake_result, perm_mode


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        pipeline=PipelineConfig(plan_timeout_sec=10, dev_timeout_sec=10, max_clarify_rounds=2),
        repos=[RepoConfig(name="demo", path=repo_root, default_branch="main")],
        limits=LimitsConfig(),
    )


@pytest.fixture
def repo_cfg(cfg: AppConfig) -> RepoConfig:
    return cfg.repos[0]


@pytest.fixture
def replies() -> list:
    """收集所有 reply 调用，便于断言。"""
    return []


@pytest.fixture
def reply(replies: list):
    async def _reply(chatid: str, text: str) -> None:
        replies.append((chatid, text))
    return _reply


@pytest.fixture(autouse=True)
def stub_git_and_mr(monkeypatch: pytest.MonkeyPatch):
    """整个文件统一 stub git / mr，避免真起子进程。"""

    async def fake_fetch(_root: Path, _branch: str, _remote: str = "origin") -> None:
        return None

    async def fake_create_worktree(_root: Path, path: Path, _branch: str, _base: str) -> None:
        path.mkdir(parents=True, exist_ok=True)

    async def fake_has_commits_ahead(_path: Path, _base: str) -> bool:
        return True

    async def fake_has_commits_ahead_remote(_alias: str, _wt: str, _base: str) -> bool:
        return True

    async def fake_has_uncommitted_changes(_path: Path) -> bool:
        return False

    async def fake_has_uncommitted_changes_remote(_alias: str, _wt: str) -> bool:
        return False

    async def fake_head_sha(_path: Path) -> str | None:
        return "sha-stub"

    async def fake_head_sha_remote(_alias: str, _wt: str) -> str:
        return "sha-stub"

    async def fake_mr_create(**_kwargs) -> str:
        return "https://gitlab/example/-/merge_requests/42"

    monkeypatch.setattr(repo_module, "fetch_default_branch", fake_fetch)
    monkeypatch.setattr(repo_module, "create_worktree", fake_create_worktree)
    monkeypatch.setattr(repo_module, "has_commits_ahead", fake_has_commits_ahead)
    monkeypatch.setattr(repo_module, "has_commits_ahead_remote", fake_has_commits_ahead_remote)
    monkeypatch.setattr(repo_module, "has_uncommitted_changes", fake_has_uncommitted_changes)
    monkeypatch.setattr(
        repo_module, "has_uncommitted_changes_remote", fake_has_uncommitted_changes_remote
    )
    monkeypatch.setattr(repo_module, "head_sha", fake_head_sha)
    monkeypatch.setattr(repo_module, "head_sha_remote", fake_head_sha_remote)
    monkeypatch.setattr(mr_module, "create_mr_via_push", fake_mr_create)


def make_claude_stub(scripted: list[str]) -> Callable:
    """生成一个按调用顺序返回 scripted 文本的假 claude_run。"""
    call_count = {"n": 0}

    async def stub(**kwargs) -> ClaudeRunResult:
        i = call_count["n"]
        call_count["n"] += 1
        text = scripted[min(i, len(scripted) - 1)]
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output=text,
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    return stub


async def test_outbound_delivery_failure_is_persisted(db, cfg, repo_cfg):
    async def failed_reply(_chatid: str, _text: str) -> None:
        raise RuntimeError("network down")

    s = Session(
        db=db,
        config=cfg,
        repo_cfg=repo_cfg,
        reply=failed_reply,
        claude_run=make_claude_stub(["unused"]),
    )
    await s.create_row(initial_request="测试投递", chatid="c1", userid="u1")
    with pytest.raises(RuntimeError, match="network down"):
        await s._notify("通知")
    messages = await db.list_messages(s.slug)
    assert messages[-1]["direction"] == "out"
    assert messages[-1]["delivery_status"] == "failed"


def make_recording_stub(scripted: list[str]) -> tuple[Callable, list[dict]]:
    """同 make_claude_stub，但把每次调用的 kwargs 记进 calls，便于断言注入的 prompt。"""
    calls: list[dict] = []

    async def stub(**kwargs) -> ClaudeRunResult:
        i = len(calls)
        calls.append(kwargs)
        text = scripted[min(i, len(scripted) - 1)]
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output=text,
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    return stub, calls


# ---- happy path ----

async def test_happy_path_to_completed(db, cfg, repo_cfg, reply, replies):
    claude_stub = make_claude_stub([
        "plan 完成。\n\nSLUG: add-readme-line\nSTATUS: READY\n",
        "开发完成，已 commit 并 push。\n\nSTATUS: READY\n",
    ])
    session = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await session.start(initial_request="加一行 readme", chatid="c1", userid="u1")

    row = await db.get_session(session.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["display_slug"] == "add-readme-line"
    assert row["mr_url"].endswith("/42")
    assert any("MR" in t for _, t in replies)

    # plan.md 已落盘且只含正文（剥协议尾）
    plan_md = cfg.workspace_root / "sessions" / session.slug / "plan.md"
    assert plan_md.exists()
    assert plan_md.read_text(encoding="utf-8") == "plan 完成。"
    # READY 路径会发"plan 已就绪 + plan.md 路径"回执
    assert any("plan 已就绪" in t and "plan.md" in t for _, t in replies)


async def test_driven_session_writes_readable_session_log(db, cfg, repo_cfg, reply):
    """驱动一条 session：session.log 落盘，含阶段流转、工具调用/守卫阻断、以及通知。

    验证「一个文件看全」的两半：claude 事件（工具输入/守卫阻断，来自 stream 事件）
    + 主控侧信息（阶段流转、plan 就绪通知，stream.jsonl 里没有）。
    """
    scripted = ["plan 完成。\n\nSLUG: demo-x\nSTATUS: READY\n", "开发完成，已 push。\n\nSTATUS: READY\n"]
    calls = {"n": 0}

    async def stub(**kwargs) -> ClaudeRunResult:
        i = calls["n"]
        calls["n"] += 1
        on_event = kwargs.get("on_event")
        if i == 0 and on_event is not None:  # plan 阶段吐一条工具调用 + 一条守卫阻断
            await on_event(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "grep -rn x src/ 2>/dev/null"},
                            }
                        ]
                    },
                }
            )
            await on_event(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": "禁止在工作目录外写入：/dev/null",
                                "is_error": True,
                            }
                        ]
                    },
                }
            )
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output=scripted[min(i, len(scripted) - 1)],
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    session = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await session.start(initial_request="做点事", chatid="c1", userid="u1")

    log = (cfg.workspace_root / "sessions" / session.slug / "session.log").read_text(
        encoding="utf-8"
    )
    assert "PLANNING" in log  # 阶段流转（stream.jsonl 里没有）
    assert "grep -rn x src/ 2>/dev/null" in log  # 工具调用输入
    assert "禁止在工作目录外写入：/dev/null" in log  # 守卫阻断（tool_result is_error）
    assert "⛔" in log
    assert "plan 已就绪" in log  # _notify 通知也缝进日志


# ---- clarification 回路 ----

async def test_clarification_then_ready(db, cfg, repo_cfg, reply, replies):
    claude_stub = make_claude_stub([
        # 第一次 plan：需要澄清
        "需要更多信息。\n\nSLUG: add-login\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n- 密码还是 OAuth？\n",
        # 用户回复后第二次 plan：ready
        "好的，明确了。\n\nSLUG: add-login\nSTATUS: READY\n",
        # dev
        "完成 ✅\n\nSTATUS: READY\n",
    ])
    session = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await session.start(initial_request="加个登录", chatid="c1", userid="u1")

    row = await db.get_session(session.slug)
    assert row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value
    assert row["clarify_rounds"] == 1
    assert row["clarify_phase"] == "planning"
    assert any("OAuth" in t for _, t in replies)
    # 澄清回执同时含问题清单与 plan.md 路径
    assert any("OAuth" in t and "plan 全文" in t and "plan.md" in t for _, t in replies)
    # 问题清单以有序编号呈现（防止退回 `- ` 无序 bullet）
    assert any("1. 密码还是 OAuth？" in t for _, t in replies)
    # 新 tag 格式：[session: <slug> @<repo> sid: <uuid>]，repo 段固定，sid 来自 row.claude_session_id
    assert any("[session: add-login @demo sid: " in t for _, t in replies)

    # 第一轮 plan.md 落盘内容为剥协议尾的正文
    plan_md = cfg.workspace_root / "sessions" / session.slug / "plan.md"
    assert plan_md.exists()
    assert plan_md.read_text(encoding="utf-8") == "需要更多信息。"

    # 用户引用回复
    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s2.resume(session.slug)
    await s2.handle_user_clarification("用密码", quote_text="[session: add-login]")

    row = await db.get_session(session.slug)
    assert row["state"] == SessionState.COMPLETED.value
    # 第二轮 plan.md 被覆盖为新一轮正文
    assert plan_md.read_text(encoding="utf-8") == "好的，明确了。"


# ---- 澄清轮数超限 ----

async def test_clarification_max_rounds_exceeded(db, cfg, repo_cfg, reply, replies):
    # max_clarify_rounds=2：第一轮+第二轮都 NEED_CLARIFICATION，第二次回复后再 plan 仍是 NC → fail
    claude_stub = make_claude_stub([
        "SLUG: x-y\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n- A\n",
        "SLUG: x-y\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n- B\n",
        "SLUG: x-y\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n- C\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="模糊需求", chatid="c1", userid="u1")
    # 第一轮回复
    await s.resume(s.slug)
    await s.handle_user_clarification("答 1")
    # 第二轮回复（应触发超限）
    await s.resume(s.slug)
    await s.handle_user_clarification("答 2")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert "澄清轮数" in (row["last_error"] or "")


# ---- dev 阶段澄清回路（NEED_CLARIFICATION → awaiting → resume developing）----

async def test_dev_clarification_then_ready(db, cfg, repo_cfg, reply, replies):
    stub, calls = make_recording_stub([
        "plan ok\n\nSLUG: add-x\nSTATUS: READY\n",
        # dev 首轮：需要用户拍板，不 commit
        "我需要你决定。\n\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n1. A 还是 B？\n",
        # 用户答复后 dev 二轮：完成
        "已按你的选择完成开发并 commit。\n\nSTATUS: READY\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="做个 x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value
    assert row["clarify_rounds"] == 1
    assert row["clarify_phase"] == "developing"
    # dev 澄清回执含问题清单，但不含 plan 阶段专有的「plan 全文」行
    assert any("1. A 还是 B？" in t for _, t in replies)
    assert not any("A 还是 B" in t and "plan 全文" in t for _, t in replies)

    # 用户引用回复 → resume 回 developing，答复注入 dev prompt
    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    await s2.handle_user_clarification("选 A", quote_text="[session: add-x]")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    # 第 3 次 claude 调用（dev 二轮）的 prompt 注入了用户答复 + 澄清回路措辞
    assert "选 A" in calls[2]["prompt"]
    assert "待确认问题" in calls[2]["prompt"]


async def test_dev_clarification_priority_over_commit(db, cfg, repo_cfg, reply, replies):
    """dev 输出既（默认 has_commits_ahead=True）有 commit 又 NEED_CLARIFICATION → 澄清优先，挂 awaiting。"""
    stub = make_claude_stub([
        "plan ok\n\nSLUG: add-y\nSTATUS: READY\n",
        "我改了几行但拿不准方向。\n\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n1. A 还是 B？\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="做个 y", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value
    assert row["clarify_phase"] == "developing"


async def test_dev_clarification_max_rounds_exceeded(db, cfg, repo_cfg, reply, replies):
    # max_clarify_rounds=2：plan READY 进 dev；dev 连续 NEED_CLARIFICATION，第 3 次 dev 触发超限 → FAILED
    stub = make_claude_stub([
        "plan ok\n\nSLUG: add-z\nSTATUS: READY\n",
        "STATUS: NEED_CLARIFICATION\nQUESTIONS:\n1. A\n",
        "STATUS: NEED_CLARIFICATION\nQUESTIONS:\n1. B\n",
        "STATUS: NEED_CLARIFICATION\nQUESTIONS:\n1. C\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="模糊 dev", chatid="c1", userid="u1")
    await s.resume(s.slug)
    await s.handle_user_clarification("答 1")
    await s.resume(s.slug)
    await s.handle_user_clarification("答 2")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert "澄清轮数" in (row["last_error"] or "")
    assert row["failed_phase"] == SessionState.DEVELOPING.value


async def test_dev_clarification_remote(db, cfg, tmp_path, reply, replies):
    repo_cfg = _make_remote_repo_cfg(tmp_path)
    stub = make_claude_stub([
        "plan ready\n\nSLUG: add-remote-x\nSTATUS: READY\n",
        # remote dev 首轮：需要用户决策
        "远端拿不准。\n\nSTATUS: NEED_CLARIFICATION\nQUESTIONS:\n1. A 还是 B？\n",
        # 答复后 dev 二轮：完成开发（有 commit）
        "已在远端完成开发并 commit。\n\nSTATUS: READY\n",
        # publish 阶段：push + MR
        "完成报告：push 完成。\n\nMR_URL: https://gitlab.example/g/r/-/merge_requests/77\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="远端做个 x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value
    assert row["clarify_phase"] == "developing"

    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    await s2.handle_user_clarification("选 A", quote_text="[session: add-remote-x]")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["mr_url"].endswith("/merge_requests/77")


# ---- plan 阶段没按协议输出 ----

async def test_plan_without_protocol_fails(db, cfg, repo_cfg, reply, replies):
    claude_stub = make_claude_stub([
        "我做了分析，但忘了输出协议字段。",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert "协议" in (row["last_error"] or "")


# ---- plan 阶段 claude 失败：上报带 result 事件真实错误 ----

async def test_plan_failure_surfaces_result_error_message(db, cfg, repo_cfg, reply, replies):
    """回归 req-20260529-191122-9409：plan 阶段 claude exit=1 且 stderr 为空，但终态
    result 事件带 is_error+错误文本。失败上报应带上该文本，而不是只报 exit=1。"""
    msg = "The model's tool call could not be parsed (retry also failed)."

    async def stub(**kwargs) -> ClaudeRunResult:
        return ClaudeRunResult(
            exit_code=1,
            session_id=kwargs["session_id"],
            text_output="",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
            result_is_error=True,
            error_message=msg,
        )

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert msg in (row["last_error"] or "")
    # 用户收到的失败通知也带真实错误文本，而非光秃秃的 exit=1
    assert any(msg in t for _, t in replies)


async def test_plan_exit_zero_but_is_error_fails(db, cfg, repo_cfg, reply, replies):
    """防御：claude 以 exit=0 退出但 result 事件 is_error=true，也应判失败。"""

    async def stub(**kwargs) -> ClaudeRunResult:
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs["session_id"],
            text_output="",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
            result_is_error=True,
            error_message="model error",
        )

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert "model error" in (row["last_error"] or "")


# ---- dev 完成但 worktree 无 commit ----

async def test_dev_without_commit_fails(db, cfg, repo_cfg, reply, replies, monkeypatch):
    """无新 commit 且 worktree 干净（真没写代码）→ 泛化文案「无新 commit」。"""
    async def fake_no_commits(_path, _base):
        return False
    monkeypatch.setattr(repo_module, "has_commits_ahead", fake_no_commits)
    # worktree 干净（默认已 stub has_uncommitted_changes=False）

    claude_stub = make_claude_stub([
        "SLUG: foo-bar\nSTATUS: READY",
        "完成（但其实没改）",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="无意义需求", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert "无新 commit" in (row["last_error"] or "")


async def test_dev_without_commit_but_dirty_reports_uncommitted(
    db, cfg, repo_cfg, reply, replies, monkeypatch
):
    """无新 commit 但 worktree 有未提交改动（写了没 commit，典型如推迟 commit 等后台构建）
    → 文案应指明「改动未提交、成果未丢」并给 worktree 路径，而非误导为「无新 commit」。"""
    async def fake_no_commits(_path, _base):
        return False
    async def fake_dirty(_path):
        return True
    monkeypatch.setattr(repo_module, "has_commits_ahead", fake_no_commits)
    monkeypatch.setattr(repo_module, "has_uncommitted_changes", fake_dirty)

    claude_stub = make_claude_stub([
        "SLUG: foo-bar\nSTATUS: READY",
        "代码改完了，等后台 CUDA 构建完成再 commit。",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="改点东西", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    err = row["last_error"] or ""
    assert "有未提交改动" in err
    assert "成果未丢" in err
    assert "无新 commit" not in err


async def test_remote_dev_without_commit_but_dirty_reports_uncommitted(
    db, cfg, tmp_path, reply, replies, monkeypatch
):
    """remote 模式同款：远端 worktree 无新 commit 但有未提交改动 → 「远端 worktree 改动未提交」文案。"""
    async def fake_no_commits_remote(_alias, _wt, _base):
        return False
    async def fake_dirty_remote(_alias, _wt):
        return True
    monkeypatch.setattr(repo_module, "has_commits_ahead_remote", fake_no_commits_remote)
    monkeypatch.setattr(repo_module, "has_uncommitted_changes_remote", fake_dirty_remote)

    repo_cfg = _make_remote_repo_cfg(tmp_path)
    claude_stub = make_claude_stub([
        "SLUG: foo-bar\nSTATUS: READY",
        "远端代码改完了，等后台构建完成再 commit。",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="改点东西", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    err = row["last_error"] or ""
    assert "远端 worktree 有未提交改动" in err
    assert "成果未丢" in err


# ---- mode=remote ----

def _make_remote_repo_cfg(tmp_path: Path) -> RepoConfig:
    shell = tmp_path / "shell"
    shell.mkdir(exist_ok=True)
    return RepoConfig(
        name="remote-demo",
        path=shell,
        default_branch="main",
        mode="remote",
        remote_ssh_alias="dev01.example",
        remote_repo_path="/home/x/demo",
        remote_worktree_root="/home/x/demo-worktrees",
    )


async def test_remote_happy_path_completed(db, cfg, tmp_path, reply, replies):
    repo_cfg = _make_remote_repo_cfg(tmp_path)
    claude_stub = make_claude_stub([
        "plan ready\n\nSLUG: add-pipeline\nSTATUS: READY\n",
        # remote：dev 只 commit，须显式声明完成
        "已在远端完成开发并 commit。\n\nSTATUS: READY\n",
        # publish 阶段：push + 建 MR
        "完成报告：在远端建了 worktree、改了代码、push 完成。\n\n"
        "MR_URL: https://gitlab.example/group/repo/-/merge_requests/99\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="加部署检查通路", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["display_slug"] == "add-pipeline"
    assert row["mr_url"] == "https://gitlab.example/group/repo/-/merge_requests/99"
    # remote 模式下 branch 应保持 None（分支由 claude 在远端建）
    assert row["branch"] is None
    assert any("merge_requests/99" in t for _, t in replies)


async def test_remote_missing_mr_url_fails(db, cfg, tmp_path, reply, replies):
    repo_cfg = _make_remote_repo_cfg(tmp_path)
    claude_stub = make_claude_stub([
        "SLUG: foo-bar\nSTATUS: READY",
        # remote dev：只 commit 并声明完成
        "已在远端完成开发并 commit。\n\nSTATUS: READY\n",
        # publish：忘了输出 MR_URL → 失败
        "完成了，但忘了输出 MR URL。",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert "MR_URL" in (row["last_error"] or "")


async def test_remote_dev_exit_nonzero_fails(db, cfg, tmp_path, reply, replies):
    """dev 阶段 claude 退出码非 0（如 ssh 失败）→ FAILED，错误带 stderr 末尾。"""
    call_count = {"n": 0}

    async def stub(**kwargs) -> ClaudeRunResult:
        i = call_count["n"]
        call_count["n"] += 1
        if i == 0:
            return ClaudeRunResult(
                exit_code=0,
                session_id=kwargs["session_id"],
                text_output="SLUG: foo-bar\nSTATUS: READY",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="",
                timed_out=False,
            )
        return ClaudeRunResult(
            exit_code=1,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="ssh: Could not resolve hostname dev01.example",
            timed_out=False,
        )

    repo_cfg = _make_remote_repo_cfg(tmp_path)
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    err = row["last_error"] or ""
    assert "ssh" in err.lower() or "exit=1" in err


# ---- MR 提交失败 ----

async def test_mr_failure_fails_session(db, cfg, repo_cfg, reply, replies, monkeypatch):
    async def fake_mr_fail(**_kwargs):
        raise mr_module.MrCreateError("403 forbidden")
    monkeypatch.setattr(mr_module, "create_mr_via_push", fake_mr_fail)

    claude_stub = make_claude_stub([
        "SLUG: foo-bar\nSTATUS: READY",
        "完成\n\nSTATUS: READY\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert "403" in (row["last_error"] or "")


# ---- follow-up 唤醒：失败/超时/已完成 session 可被引用回复继续推进 ----


async def test_followup_failed_in_developing_resumes_developing(
    db, cfg, repo_cfg, reply, replies, monkeypatch
):
    """dev 阶段失败的 session（failed_phase=developing）→ followup → 回到 DEVELOPING；
    用户消息以"追加反馈"形式注入 dev prompt 给 claude；二次开发完成后落 COMPLETED。"""
    captured_prompts: list[str] = []

    call_count = {"n": 0}

    async def stub(**kwargs):
        captured_prompts.append(kwargs.get("prompt", ""))
        i = call_count["n"]
        call_count["n"] += 1
        if i == 0:
            text = "SLUG: fix-bug\nSTATUS: READY\n"
        elif i == 1:
            # dev 第一次：异常退出 → _fail（phase=developing）
            return ClaudeRunResult(
                exit_code=1,
                session_id=kwargs.get("resume_from") or kwargs["session_id"],
                text_output="",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="something broke",
                timed_out=False,
            )
        else:
            text = "二次开发完成 ✅\n\nSTATUS: READY\n"
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output=text,
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="修个 bug", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert row["failed_phase"] == SessionState.DEVELOPING.value

    # followup：用户引用失败回执回复
    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    await s2.handle_user_followup("commit 漏了 build 产物，重试一下")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    # dev 第二次 prompt 应包含用户的追加反馈
    dev_followup_prompt = captured_prompts[2]
    assert "追加反馈" in dev_followup_prompt
    assert "commit 漏了 build 产物" in dev_followup_prompt


async def test_followup_completed_session_routes_to_developing(
    db, cfg, repo_cfg, reply, replies
):
    """已 COMPLETED 的 session 收到 followup → 走 DEVELOPING，把 followup 注入为
    "用户对上一轮开发结果的追加反馈" dev prompt；不再二次走 plan。"""
    captured: list[dict] = []

    async def stub(**kwargs):
        captured.append({"mode": perm_mode(kwargs), "prompt": kwargs.get("prompt", "")})
        return fake_result(
            kwargs,
            "SLUG: tweak-readme\nSTATUS: READY\n"
            if perm_mode(kwargs) == "plan"
            else "完成 ✅\n\nSTATUS: READY\n",
        )

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加一行 readme", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    # happy path 应只有两次 claude 调用：plan + dev
    assert len(captured) == 2
    assert [c["mode"] for c in captured] == ["plan", "acceptEdits"]

    # followup：用户对已完成的 MR 提追加诉求
    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    await s2.handle_user_followup("再加一行致谢")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    # 第三次调用应是 dev，而不是 plan
    assert captured[2]["mode"] == "acceptEdits"
    # 整轮唤醒不应再次进入 plan permission_mode
    assert not any(c["mode"] == "plan" for c in captured[2:])
    # followup 应被注入为"追加反馈"风格 dev prompt
    assert "追加反馈" in captured[2]["prompt"]
    assert "再加一行致谢" in captured[2]["prompt"]


async def test_followup_completed_with_operational_request_goes_dev(
    db, cfg, repo_cfg, reply, replies
):
    """回归：复现线上 session review-and-tune-http-filters 的 bug ——
    COMPLETED 后收到"解决冲突"类操作型 followup 时，不应再走 plan permission_mode
    （强协议 + 禁写文件），而是直接在已有 worktree 上续 dev。"""
    captured_modes: list[str] = []

    async def stub(**kwargs):
        mode = perm_mode(kwargs)
        captured_modes.append(mode)
        return fake_result(
            kwargs,
            "SLUG: fix-http-filters\nSTATUS: READY\n" if mode == "plan" else "完成 ✅\n\nSTATUS: READY\n",
        )

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="review 项目并按最近 MR 调整 http 筛选项",
                  chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value

    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)

    # apply_followup 应当把 COMPLETED 切到 DEVELOPING（而不是 PLANNING）
    ok = await s2.apply_followup("有 merge 冲突。解决冲突")
    assert ok is True
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.DEVELOPING.value

    # drive 完后落回 COMPLETED；唤醒后的 claude 调用全部是 dev 模式
    await s2.drive()
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert captured_modes[2] == "acceptEdits"
    assert "plan" not in captured_modes[2:]


async def test_followup_failed_in_new_phase_is_rejected(
    db, cfg, repo_cfg, reply, replies, monkeypatch
):
    """worktree 创建阶段失败的 session（failed_phase=new）→ followup 拒绝，
    给出"环境创建阶段失败"提示；不切状态，不起 drive。"""
    async def fake_create_worktree_fails(_root, _path, _branch, _base):
        raise repo_module.GitError("permission denied")
    monkeypatch.setattr(repo_module, "create_worktree", fake_create_worktree_fails)

    claude_stub = make_claude_stub(["unreachable"])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert row["failed_phase"] == SessionState.NEW.value

    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s2.resume(s.slug)
    ok = await s2.apply_followup("重试一下")
    assert ok is False
    assert s2._last_followup_notice is not None
    assert "环境创建" in s2._last_followup_notice

    # 状态不应被切回 working
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value


async def test_followup_worktree_missing_is_rejected(
    db, cfg, repo_cfg, reply, replies
):
    """failed_phase=developing 但 worktree 目录已被手动删除 → followup 拒绝，提示丢失。"""
    async def stub(**kwargs):
        # 让 dev 失败，给 session 一个 worktree 路径但接下来要删掉
        if perm_mode(kwargs) == "plan":
            return fake_result(kwargs, "SLUG: foo-bar\nSTATUS: READY\n")
        return fake_result(kwargs, "", exit_code=1, stderr_tail="broken")

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    wt = Path(row["worktree_path"])
    assert wt.is_dir()

    # 模拟用户手动 git worktree remove
    import shutil
    shutil.rmtree(wt)

    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    ok = await s2.apply_followup("继续吧")
    assert ok is False
    assert s2._last_followup_notice is not None
    assert "worktree" in s2._last_followup_notice or "丢失" in s2._last_followup_notice


async def test_followup_cancelled_is_rejected(db, cfg, repo_cfg, reply, replies):
    """CANCELLED session 不属于 resumable terminal → followup 直接返回 False，
    不输出 _last_followup_notice（由 dispatcher 上游决定走 NEW，不该到这里）。"""
    claude_stub = make_claude_stub(["plan", "dev"])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.create_row(initial_request="x", chatid="c1", userid="u1")
    await s.cancel("用户测试取消")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.CANCELLED.value

    ok = await s.apply_followup("继续")
    assert ok is False
    assert s._last_followup_notice is None  # 不是"拒绝并提示"，是上游路由错误，静默


# ---- cancel 终态吸收：防 dev/_fail 等覆盖 CANCELLED 导致继续推 MR ----

async def test_cancel_during_dev_blocks_mr_submission(
    db, cfg, repo_cfg, reply, replies, monkeypatch
):
    """回归：dev 阶段 /cancel 但仍提交了 MR。

    cancel 写完 CANCELLED 后，dev 子进程自然跑完，``_do_developing`` 末尾的
    ``_set_state(MR_SUBMITTING)`` 必须被守卫吸收 —— ``create_mr_via_push`` 不应
    被调用，也不应发"开发完成 ✅"通知。
    """
    mr_calls: list = []

    async def spy_mr_create(**kwargs) -> str:
        mr_calls.append(kwargs)
        return "https://gitlab/example/-/merge_requests/should-not-be-called"

    monkeypatch.setattr(mr_module, "create_mr_via_push", spy_mr_create)

    session_box: dict = {}
    call_count = {"n": 0}

    async def stub(**kwargs) -> ClaudeRunResult:
        i = call_count["n"]
        call_count["n"] += 1
        if i == 1:
            # dev 阶段：模拟"claude 子进程还在跑时用户已发 /cancel"
            await session_box["s"].cancel("用户测试取消")
            text = "dev done"
        else:
            text = "plan ok\n\nSLUG: cancel-race\nSTATUS: READY\n"
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output=text,
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    session_box["s"] = s
    await s.start(initial_request="加一行", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.CANCELLED.value
    assert mr_calls == [], "cancel 后绝不能去推 MR"
    assert not any("开发完成" in t for _, t in replies)
    assert any("已取消" in t for _, t in replies)


async def test_cancel_blocks_fail_overwrite(db, cfg, repo_cfg, reply, replies):
    """cancel 写 CANCELLED 后，dev 阶段 claude exit≠0 走 _fail，
    ``_set_state(FAILED)`` 必须被守卫吸收（状态稳定在 CANCELLED），且 ``_fail`` 的
    "❌ session 失败"通知必须被 ``_notify`` 的 CANCELLED 守卫抑制——只保留"已取消"。"""
    session_box: dict = {}
    call_count = {"n": 0}

    async def stub(**kwargs) -> ClaudeRunResult:
        i = call_count["n"]
        call_count["n"] += 1
        if i == 0:
            return ClaudeRunResult(
                exit_code=0,
                session_id=kwargs["session_id"],
                text_output="ok\n\nSLUG: cancel-then-fail\nSTATUS: READY\n",
                stream_log_path=kwargs["stream_log_path"],
                stderr_tail="",
                timed_out=False,
            )
        # dev：先模拟 cancel，再返回非零 exit_code 触发 _fail 路径
        await session_box["s"].cancel("用户测试取消")
        return ClaudeRunResult(
            exit_code=1,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="boom",
            timed_out=False,
        )

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    session_box["s"] = s
    await s.start(initial_request="x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.CANCELLED.value
    assert not any("失败" in t or "❌" in t for _, t in replies), "cancel 后不应再发失败通知"
    assert any("已取消" in t for _, t in replies)


async def test_cancel_suppresses_plan_ready_notice(db, cfg, repo_cfg, reply, replies):
    """plan 阶段 /cancel：plan 子进程跑完后 ``_do_planning`` 仍会走到
    ``_notify_plan_ready``（plan-review 默认关闭），这条"plan 已就绪，开始开发"通知
    必须被 ``_notify`` 的 CANCELLED 守卫抑制——只保留"已取消"，且不真正进入开发。"""
    session_box: dict = {}

    async def stub(**kwargs) -> ClaudeRunResult:
        # plan 阶段：模拟"claude 子进程还在跑时用户已发 /cancel"
        await session_box["s"].cancel("用户测试取消")
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output="plan ok\n\nSLUG: cancel-plan\nSTATUS: READY\n",
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    session_box["s"] = s
    await s.start(initial_request="加一行", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.CANCELLED.value
    assert not any("plan 已就绪" in t or "开始开发" in t for _, t in replies)
    assert any("已取消" in t for _, t in replies)


async def test_cancel_suppresses_clarification_notice(db, cfg, repo_cfg, reply, replies):
    """plan 阶段 /cancel + plan 输出 NEED_CLARIFICATION：``_notify_clarification`` 的
    "需要进一步确认"提问通知必须被守卫抑制，不让用户误以为还需补充信息。"""
    session_box: dict = {}

    async def stub(**kwargs) -> ClaudeRunResult:
        await session_box["s"].cancel("用户测试取消")
        return ClaudeRunResult(
            exit_code=0,
            session_id=kwargs.get("resume_from") or kwargs["session_id"],
            text_output=(
                "需要澄清\n\nSLUG: cancel-clarify\nSTATUS: NEED_CLARIFICATION\n"
                "QUESTIONS:\n- 目标分支是哪个？\n"
            ),
            stream_log_path=kwargs["stream_log_path"],
            stderr_tail="",
            timed_out=False,
        )

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    session_box["s"] = s
    await s.start(initial_request="x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.CANCELLED.value
    assert not any("需要进一步确认" in t for _, t in replies)
    assert any("已取消" in t for _, t in replies)


# ---- MR 元数据：claude 按协议输出 vs git log 兜底 ----

async def test_mr_metadata_from_claude_protocol(
    db, cfg, repo_cfg, reply, replies, monkeypatch: pytest.MonkeyPatch
):
    """dev 输出含 MR_TITLE / MR_DESCRIPTION 协议块时，主控应把解析后的值
    传给 create_mr_via_push，而非用 initial_request 首行兜底。"""
    captured: dict = {}

    async def fake_mr_create(**kwargs) -> str:
        captured.update(kwargs)
        return "https://gitlab/example/-/merge_requests/77"

    monkeypatch.setattr(mr_module, "create_mr_via_push", fake_mr_create)

    dev_output = (
        "完成报告：已在 README 顶部新增一行项目简介。\n\n"
        "STATUS: READY\n\n"
        "MR_TITLE: feat: 在 README 顶部新增项目简介行\n\n"
        "MR_DESCRIPTION_BEGIN\n"
        "## 背景\n"
        "用户希望 README 顶部能一眼看到项目用途。\n\n"
        "## 用户原始需求\n"
        "> 加一行 readme\n\n"
        "## 改动概要\n"
        "- README.md 顶部插入一行项目简介\n\n"
        "## 测试与验证\n"
        "- markdown 渲染本地确认\n\n"
        "## 文档与注释同步\n"
        "- 不涉及其它文档\n\n"
        "## 风险与回滚\n"
        "- 无显著风险\n"
        "MR_DESCRIPTION_END\n"
    )
    claude_stub = make_claude_stub([
        "plan 完成。\n\nSLUG: add-readme-line\nSTATUS: READY\n",
        dev_output,
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="加一行 readme", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert captured["title"] == "feat: 在 README 顶部新增项目简介行"
    # 描述应包含按模板写的多小节而不是 initial_request 原话兜底
    assert "## 背景" in captured["description"]
    assert "## 测试与验证" in captured["description"]
    assert "## 文档与注释同步" in captured["description"]
    # 兜底提示不该出现
    assert "主控兜底生成" not in captured["description"]


async def test_mr_metadata_fallback_via_git_log(
    db, cfg, repo_cfg, reply, replies, monkeypatch: pytest.MonkeyPatch
):
    """dev 输出未按协议给出 MR 元数据时，主控应回退到 git log 拼装的兜底版本：
    title = 最近一条 commit subject；description 含兜底标记 + commit log 列表 +
    必含的所有小节占位（即便标注"未由 claude 提供"）。"""
    captured: dict = {}

    async def fake_mr_create(**kwargs) -> str:
        captured.update(kwargs)
        return "https://gitlab/example/-/merge_requests/88"

    async def fake_subjects(_path: Path, _base: str) -> list[str]:
        # git log 默认新→旧，subjects[0] 是最新一条
        return [
            "feat: 在 README 顶部新增项目简介行",
            "chore: 调整 lint 配置",
        ]

    monkeypatch.setattr(mr_module, "create_mr_via_push", fake_mr_create)
    monkeypatch.setattr(repo_module, "get_commits_ahead_subjects", fake_subjects)

    claude_stub = make_claude_stub([
        "plan 完成。\n\nSLUG: add-readme-line\nSTATUS: READY\n",
        "开发完成。（忘了输出 MR 元数据协议）\n\nSTATUS: READY\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="加一行 readme", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    # 标题取最新一条 commit subject，而非用户原话"加一行 readme"
    assert captured["title"] == "feat: 在 README 顶部新增项目简介行"
    # 描述带兜底标记 + 全部强制小节 + commit subject 落进改动概要
    desc = captured["description"]
    assert "主控兜底生成" in desc
    assert "## 背景" in desc
    assert "## 用户原始需求" in desc
    assert "## 改动概要" in desc
    assert "## 测试与验证" in desc
    assert "## 文档与注释同步" in desc
    assert "## 风险与回滚" in desc
    assert "feat: 在 README 顶部新增项目简介行" in desc
    assert "chore: 调整 lint 配置" in desc
    # 用户需求被 quote 进描述里
    assert "> 加一行 readme" in desc


async def test_mr_metadata_fallback_when_no_commit_log(
    db, cfg, repo_cfg, reply, replies, monkeypatch: pytest.MonkeyPatch
):
    """协议缺失 + git log 也拿不到 subject 时，title 应退到 initial_request 首行（最末级兜底）。"""
    captured: dict = {}

    async def fake_mr_create(**kwargs) -> str:
        captured.update(kwargs)
        return "https://gitlab/example/-/merge_requests/99"

    async def fake_subjects(_path: Path, _base: str) -> list[str]:
        return []

    monkeypatch.setattr(mr_module, "create_mr_via_push", fake_mr_create)
    monkeypatch.setattr(repo_module, "get_commits_ahead_subjects", fake_subjects)

    claude_stub = make_claude_stub([
        "plan 完成。\n\nSLUG: x-y\nSTATUS: READY\n",
        "完成。\n\nSTATUS: READY\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="加一行 readme", chatid="c1", userid="u1")

    assert captured["title"] == "加一行 readme"


# ---- 独立 Reviewer（plan / code 审查） ----


def _reviewer_repo_cfg(base: RepoConfig, *, max_rounds: int = 1) -> RepoConfig:
    """在已有 repo 基础上启用 Reviewer。"""
    return RepoConfig(
        name=base.name,
        path=base.path,
        default_branch=base.default_branch,
        reviewer=ReviewerConfig(enabled=True, max_rounds=max_rounds),
    )


def make_role_aware_stub(
    *,
    plan_texts: list[str],
    review_texts: list[str],
    dev_texts: list[str],
    publish_texts: list[str] | None = None,
):
    """按调用角色分流的假 runner.run（单个 stub，经 ``claude_run=`` 注入）。

    归一接口下不再有 append_system_prompt_file 路径可分流；改用：
    - reviewer 调用：on_event 回调是 ``_persist_reviewer_event``（名字含 "reviewer"），
      与 Coder 的 ``_persist_claude_event`` 区分——这正是 session 区分两个 agent 事件的机制。
    - 发布调用：prompt 含「执行发布」。
    - Coder plan：permission READ_ONLY 且非上述。
    - Coder dev：permission WRITE 且非发布。

    每类按各自脚本顺序返回；记录所有调用到 ``stub.calls`` 供断言（字段不变）。
    """
    if publish_texts is None:
        publish_texts = [
            "发布完成。\n\nMR_URL: https://gitlab.example/group/repo/-/merge_requests/7\n"
        ]
    # dev 完成正向信号：主控现在要求 dev 做完必须显式输出 STATUS: READY，否则挂起等确认。
    # helper 默认模拟「合规的已完成 dev 轮」——给每条 dev_text 补 STATUS: READY（除非已显式
    # 含 STATUS 行，如个别用例要测 NEED_CLARIFICATION）。
    dev_texts = [
        t if "STATUS:" in t else (t.rstrip("\n") + "\n\nSTATUS: READY\n")
        for t in dev_texts
    ]
    counters = {"plan": 0, "review": 0, "dev": 0, "publish": 0}
    calls: list[dict] = []

    async def stub(**kwargs):
        on_event = kwargs.get("on_event")
        is_review = on_event is not None and "reviewer" in getattr(on_event, "__name__", "")
        if is_review:
            kind, texts = "review", review_texts
        elif "执行发布" in kwargs.get("prompt", ""):
            kind, texts = "publish", publish_texts
        elif kwargs.get("permission") is AgentPermission.READ_ONLY:
            kind, texts = "plan", plan_texts
        else:
            kind, texts = "dev", dev_texts
        i = counters[kind]
        counters[kind] += 1
        calls.append(
            {
                "kind": kind,
                "mode": perm_mode(kwargs),
                "prompt": kwargs.get("prompt", ""),
                "session_id": kwargs.get("session_id"),
                "resume_from": kwargs.get("resume_from"),
            }
        )
        return fake_result(kwargs, texts[min(i, len(texts) - 1)])

    stub.calls = calls  # type: ignore[attr-defined]
    return stub


async def test_plan_review_needs_revision_then_proceeds(db, cfg, reply, replies):
    """plan 审查 NEEDS_REVISION → Coder 修订 → 进开发；code 审查 APPROVED → Coder 据可选建议
    再调一轮 → MR_SUBMITTING → COMPLETED。

    code APPROVED 现在也会触发 Coder 再修一轮（捞回 Reviewer 在 APPROVED 正文里的 nit 建议），
    且下次跳过审查直接进 MR 提交；APPROVED 不计入 code_review_rounds。
    """
    repo_cfg = _reviewer_repo_cfg(cfg.repos[0])
    stub = make_role_aware_stub(
        plan_texts=[
            "初版 plan。\n\nSLUG: foo-bar\nSTATUS: READY\n",      # 首次 plan
            "完善后的 plan。\n\nSLUG: foo-bar\nSTATUS: READY\n",  # Reviewer 意见后修订
        ],
        review_texts=[
            "## 意见\n- 漏了空值处理\n\nREVIEW_VERDICT: NEEDS_REVISION\n",       # plan 审查
            "代码没问题，但建议补个 README。\n\nREVIEW_VERDICT: APPROVED\n",     # code 审查
        ],
        dev_texts=[
            "开发完成 ✅\n",                                # 首次 dev
            "已据可选建议补 README。\n",                    # code APPROVED 后微调
        ],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加个功能", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["plan_review_rounds"] == 1   # NEEDS_REVISION 计入
    assert row["code_review_rounds"] == 0   # APPROVED 不计入
    # plan(初版) → plan审(NEEDS_REVISION) → plan(修订) → dev → code审(APPROVED) → dev(微调)
    kinds = [c["kind"] for c in stub.calls]
    assert kinds == ["plan", "review", "plan", "dev", "review", "dev"]
    # Reviewer 会话独立于 Coder：reviewer_session_id 非空且 != claude_session_id
    assert row["reviewer_session_id"]
    assert row["reviewer_session_id"] != row["claude_session_id"]
    # 修订 plan 的 prompt 应带 Reviewer 审查意见（NEEDS_REVISION 语气）
    revise_plan_prompt = stub.calls[2]["prompt"]
    assert "Reviewer 审查意见" in revise_plan_prompt
    assert "漏了空值处理" in revise_plan_prompt
    assert "已审查通过" not in revise_plan_prompt  # NEEDS_REVISION 不应出现 APPROVED 语气
    # code APPROVED 后第二次 dev 的 prompt：带"已审查通过"语气 + 审查正文（含 nit）
    polish_dev_prompt = stub.calls[5]["prompt"]
    assert "已审查通过" in polish_dev_prompt
    assert "Reviewer 审查意见" in polish_dev_prompt
    assert "建议补个 README" in polish_dev_prompt
    # code 审查复用 Reviewer 会话（resume 而非新建）
    code_review_call = stub.calls[4]
    assert code_review_call["resume_from"] == row["reviewer_session_id"]
    # 审查产物落盘
    sdir = cfg.workspace_root / "sessions" / s.slug
    assert (sdir / "plan_review.md").read_text(encoding="utf-8").startswith("## 意见")
    assert (sdir / "code_review.md").exists()
    # 通知里同时出现"审查通过"与"Coder 据可选建议"措辞
    assert any("Reviewer" in t for _, t in replies)
    assert any("代码审查通过" in t and "Coder 据可选建议" in t for _, t in replies)


async def test_code_review_needs_revision_then_proceeds(db, cfg, reply, replies):
    """plan APPROVED → Coder 据可选建议再完善一轮 → dev → code 审查 NEEDS_REVISION → Coder 修订
    实现 → 轮次用尽不再审 → MR → COMPLETED。

    plan APPROVED 现在也触发 Coder 再修一轮 plan（不计 rounds、下次跳过审查）。
    """
    repo_cfg = _reviewer_repo_cfg(cfg.repos[0])
    stub = make_role_aware_stub(
        plan_texts=[
            "初版 plan。\n\nSLUG: foo-bar\nSTATUS: READY\n",            # 首次 plan
            "完善后的 plan。\n\nSLUG: foo-bar\nSTATUS: READY\n",        # plan APPROVED 后微调
        ],
        review_texts=[
            "plan 没问题，建议补一句背景。\n\nREVIEW_VERDICT: APPROVED\n",  # plan 审查通过（带 nit）
            "## 意见\n- 缺单测\n\nREVIEW_VERDICT: NEEDS_REVISION\n",        # code 审查打回
        ],
        dev_texts=["开发完成 ✅\n", "已补单测 ✅\n"],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加个功能", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["plan_review_rounds"] == 0   # APPROVED 不计入
    assert row["code_review_rounds"] == 1
    kinds = [c["kind"] for c in stub.calls]
    # plan(初版) → plan审(APPROVED) → plan(微调) → dev → code审(NEEDS_REVISION) → dev(修订)
    # （code 轮次用尽，第二次 dev 后跳过 code 审查直接进 MR）
    assert kinds == ["plan", "review", "plan", "dev", "review", "dev"]
    # plan 微调 prompt 应带"已审查通过"语气
    polish_plan_prompt = stub.calls[2]["prompt"]
    assert "已审查通过" in polish_plan_prompt
    assert "建议补一句背景" in polish_plan_prompt
    # code NEEDS_REVISION 后第二次 dev 的 prompt：常规修订语气，不应有 APPROVED 字样
    revise_dev_prompt = stub.calls[5]["prompt"]
    assert "Reviewer 审查意见" in revise_dev_prompt
    assert "缺单测" in revise_dev_prompt
    assert "已审查通过" not in revise_dev_prompt


async def test_reviewer_failure_is_skipped(db, cfg, reply, replies):
    """Reviewer 调用抛异常 → 跳过审查，当作没有 Reviewer，session 照常 COMPLETED、不 FAILED。"""
    repo_cfg = _reviewer_repo_cfg(cfg.repos[0])

    plan_count = {"n": 0}

    async def stub(**kwargs):
        on_event = kwargs.get("on_event")
        if on_event is not None and "reviewer" in getattr(on_event, "__name__", ""):
            raise RuntimeError("reviewer 子进程崩了")
        text = (
            "SLUG: foo-bar\nSTATUS: READY\n"
            if perm_mode(kwargs) == "plan"
            else "完成 ✅\n\nSTATUS: READY\n"
        )
        return fake_result(kwargs, text)

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加个功能", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["plan_review_rounds"] == 0
    assert row["code_review_rounds"] == 0
    # 跳过提示发给用户
    assert any("跳过" in t for _, t in replies)


async def test_reviewer_no_verdict_is_skipped(db, cfg, reply, replies):
    """Reviewer 输出没有 REVIEW_VERDICT 协议行 → 视为失败 → 跳过。"""
    repo_cfg = _reviewer_repo_cfg(cfg.repos[0])
    stub = make_role_aware_stub(
        plan_texts=["SLUG: foo-bar\nSTATUS: READY\n"],
        review_texts=["我觉得还行但忘了输出 verdict\n"],
        dev_texts=["完成 ✅\n"],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加个功能", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["plan_review_rounds"] == 0


async def test_reviewer_disabled_no_review_calls(db, cfg, repo_cfg, reply, replies):
    """未启用 Reviewer（默认）时不应有任何 review 调用，流程与现状一致。"""
    stub = make_role_aware_stub(
        plan_texts=["SLUG: foo-bar\nSTATUS: READY\n"],
        review_texts=["不该被调用\n\nREVIEW_VERDICT: NEEDS_REVISION\n"],
        dev_texts=["完成 ✅\n"],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加个功能", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert all(c["kind"] != "review" for c in stub.calls)
    assert row["reviewer_session_id"] is None


async def test_plan_and_code_review_both_approved_coder_polishes_each(db, cfg, reply, replies):
    """plan + code 两轮审查都 APPROVED：Reviewer 在 APPROVED 正文里给的可选建议，主控仍交回 Coder
    各完善 / 调整一轮，但**不**触发第二次审查（一次性 skip 闸门生效），最终 COMPLETED。

    这是改造的核心收益场景：原逻辑下 APPROVED 直接放行会丢 Reviewer 的 nit；新逻辑下两条路径
    都让 Coder 再修一轮，rounds 列保持 0（与 max_rounds 上限语义不冲突）。
    """
    repo_cfg = _reviewer_repo_cfg(cfg.repos[0])
    stub = make_role_aware_stub(
        plan_texts=[
            "初版 plan。\n\nSLUG: foo-bar\nSTATUS: READY\n",
            "据可选建议完善后的 plan。\n\nSLUG: foo-bar\nSTATUS: READY\n",
        ],
        review_texts=[
            "plan 不错，建议在背景补一句动机。\n\nREVIEW_VERDICT: APPROVED\n",     # plan 审查通过 + nit
            "代码 ok，建议变量名 x 改为 count。\n\nREVIEW_VERDICT: APPROVED\n",   # code 审查通过 + nit
        ],
        dev_texts=[
            "开发完成 ✅\n",
            "据可选建议改了变量名 ✅\n",
        ],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加个功能", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    # APPROVED 路径不计入 rounds
    assert row["plan_review_rounds"] == 0
    assert row["code_review_rounds"] == 0
    kinds = [c["kind"] for c in stub.calls]
    # plan(初版) → plan审(APPROVED) → plan(微调) → dev → code审(APPROVED) → dev(微调)
    # 关键：reviewer 仅被调 2 次（每阶段一次），第二轮 plan / dev 后均跳过审查直接放行
    assert kinds == ["plan", "review", "plan", "dev", "review", "dev"]
    assert sum(1 for k in kinds if k == "review") == 2
    # 两次 Coder 微调 prompt 都带"已审查通过"语气
    plan_polish_prompt = stub.calls[2]["prompt"]
    code_polish_prompt = stub.calls[5]["prompt"]
    assert "已审查通过" in plan_polish_prompt and "补一句动机" in plan_polish_prompt
    assert "已审查通过" in code_polish_prompt and "count" in code_polish_prompt
    # 通知里同时出现 plan 与 code 的"审查通过 ✅"措辞
    reply_texts = [t for _, t in replies]
    assert any("Reviewer 审查通过 ✅" in t and "Coder 据可选建议最终完善 plan" in t for t in reply_texts)
    assert any("Reviewer 代码审查通过 ✅" in t and "Coder 据可选建议最终调整代码" in t for t in reply_texts)
    # 审查产物落盘
    sdir = cfg.workspace_root / "sessions" / s.slug
    assert (sdir / "plan_review.md").exists()
    assert (sdir / "code_review.md").exists()


# ---------- remote 代码审查（defer-push：dev 只 commit → 审 → 单独 publish） ----------


def _remote_reviewer_repo_cfg(tmp_path: Path, *, enabled: bool = True, max_rounds: int = 1) -> RepoConfig:
    base = _make_remote_repo_cfg(tmp_path)
    return RepoConfig(
        name=base.name,
        path=base.path,
        default_branch=base.default_branch,
        mode="remote",
        remote_ssh_alias=base.remote_ssh_alias,
        remote_repo_path=base.remote_repo_path,
        remote_worktree_root=base.remote_worktree_root,
        reviewer=ReviewerConfig(enabled=enabled, max_rounds=max_rounds),
    )


async def test_remote_review_on_runs_code_review_then_publishes(db, cfg, tmp_path, reply, replies):
    """remote + review on：plan审 APPROVED → Coder 微调 → dev(只commit) → code审 APPROVED →
    Coder 微调 → publish → COMPLETED。

    plan / code APPROVED 都触发 Coder 再修一轮（不计 rounds，下次跳过审查），与 local 同构。
    """
    repo_cfg = _remote_reviewer_repo_cfg(tmp_path, enabled=True)
    stub = make_role_aware_stub(
        plan_texts=[
            "SLUG: add-pipeline\nSTATUS: READY\n",        # 首次 plan
            "完善后。\n\nSLUG: add-pipeline\nSTATUS: READY\n",  # plan APPROVED 后微调
        ],
        review_texts=[
            "plan 没问题。\n\nREVIEW_VERDICT: APPROVED\n",   # plan 审查
            "代码没问题。\n\nREVIEW_VERDICT: APPROVED\n",     # code 审查
        ],
        dev_texts=[
            "已在远端 commit。\n",                # 首次 dev
            "已据可选建议再调一轮 commit。\n",    # code APPROVED 后微调
        ],
        publish_texts=["已 push 并建 MR。\n\nMR_URL: https://gitlab.example/g/r/-/merge_requests/9\n"],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加部署检查", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    kinds = [c["kind"] for c in stub.calls]
    # plan(初版) → plan审(APPROVED) → plan(微调) → dev → code审(APPROVED) → dev(微调) → publish
    assert kinds == ["plan", "review", "plan", "dev", "review", "dev", "publish"]
    assert row["mr_url"] == "https://gitlab.example/g/r/-/merge_requests/9"
    assert row["plan_review_rounds"] == 0  # APPROVED 不计入
    assert row["code_review_rounds"] == 0  # APPROVED 不计入
    # code 审查（第 5 次调用，index=4）用 remote 协议、且 diff 命令经 ssh
    code_review_call = stub.calls[4]
    assert "ssh" in code_review_call["prompt"] and "git diff" in code_review_call["prompt"]
    # 审查产物落盘
    assert (cfg.workspace_root / "sessions" / s.slug / "code_review.md").exists()


async def test_remote_code_review_needs_revision_then_publishes(db, cfg, tmp_path, reply, replies):
    """remote：plan APPROVED → Coder 微调 → dev → code 审打回 → dev 修订 → 复审 APPROVED → dev
    微调 → publish。

    plan APPROVED 触发一轮 plan 微调（不计 rounds）；code 复审 APPROVED 触发一轮 dev 微调；
    最终 code_review_rounds=1（NEEDS_REVISION 计入）。
    """
    repo_cfg = _remote_reviewer_repo_cfg(tmp_path, enabled=True, max_rounds=2)
    stub = make_role_aware_stub(
        plan_texts=[
            "SLUG: add-x\nSTATUS: READY\n",          # 首次 plan
            "完善后。\n\nSLUG: add-x\nSTATUS: READY\n",  # plan APPROVED 后微调
        ],
        review_texts=[
            "plan ok。\n\nREVIEW_VERDICT: APPROVED\n",                 # plan 审查（带 nit）
            "## 意见\n- 缺单测\n\nREVIEW_VERDICT: NEEDS_REVISION\n",  # code 审查打回
            "补了单测，建议也加个 CHANGELOG。\n\nREVIEW_VERDICT: APPROVED\n",  # code 复审通过（带 nit）
        ],
        dev_texts=[
            "首次 commit。\n",
            "据意见补 commit。\n",
            "据可选建议再调一轮 commit。\n",  # code 复审 APPROVED 后的微调
        ],
        publish_texts=["MR_URL: https://gitlab.example/g/r/-/merge_requests/11\n"],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加功能", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    kinds = [c["kind"] for c in stub.calls]
    # plan → plan审(APPROVED) → plan(微调) → dev → code审(NEEDS_REVISION) → dev(修订) →
    # code审(APPROVED) → dev(微调) → publish
    assert kinds == [
        "plan", "review", "plan", "dev", "review", "dev", "review", "dev", "publish",
    ]
    assert row["plan_review_rounds"] == 0  # APPROVED 不计入
    assert row["code_review_rounds"] == 1  # NEEDS_REVISION 计入一次
    # 修订 dev 的 prompt（NEEDS_REVISION 触发，index=5）带 Reviewer 意见，且明确仍不要 push
    revise_dev = stub.calls[5]["prompt"]
    assert "Reviewer 审查意见" in revise_dev and "缺单测" in revise_dev
    assert "不要 push" in revise_dev
    assert "已审查通过" not in revise_dev  # NEEDS_REVISION 路径不该有 APPROVED 语气
    # code 复审 APPROVED 触发的 dev 微调（index=7）：带 APPROVED 语气
    polish_dev = stub.calls[7]["prompt"]
    assert "已审查通过" in polish_dev
    assert "CHANGELOG" in polish_dev
    assert row["mr_url"] == "https://gitlab.example/g/r/-/merge_requests/11"


async def test_remote_review_off_still_publishes(db, cfg, tmp_path, reply, replies):
    """remote + review off（方案2 全拆）：dev(只commit) → publish，无任何审查调用。"""
    repo_cfg = _remote_reviewer_repo_cfg(tmp_path, enabled=False)
    stub = make_role_aware_stub(
        plan_texts=["SLUG: add-y\nSTATUS: READY\n"],
        review_texts=["不该被调用\n\nREVIEW_VERDICT: NEEDS_REVISION\n"],
        dev_texts=["已 commit。\n"],
        publish_texts=["MR_URL: https://gitlab.example/g/r/-/merge_requests/13\n"],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="改个小东西", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    kinds = [c["kind"] for c in stub.calls]
    assert kinds == ["plan", "dev", "publish"]
    assert all(c["kind"] != "review" for c in stub.calls)
    assert row["mr_url"] == "https://gitlab.example/g/r/-/merge_requests/13"


async def test_remote_publish_missing_mr_url_fails(db, cfg, tmp_path, reply, replies):
    """remote 发布阶段未吐 MR_URL → FAILED，failed_phase=mr_submitting（便于引用回复重试发布）。"""
    repo_cfg = _remote_reviewer_repo_cfg(tmp_path, enabled=False)
    stub = make_role_aware_stub(
        plan_texts=["SLUG: add-z\nSTATUS: READY\n"],
        review_texts=["x"],
        dev_texts=["已 commit。\n"],
        publish_texts=["我 push 了但忘了输出 URL。\n"],  # 缺 MR_URL
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加东西", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert row["failed_phase"] == SessionState.MR_SUBMITTING.value


async def test_remote_code_review_failure_skips_to_publish(db, cfg, tmp_path, reply, replies):
    """remote：审查调用抛异常 → 跳过审查 → 仍 publish → COMPLETED，不 FAILED。"""
    repo_cfg = _remote_reviewer_repo_cfg(tmp_path, enabled=True)

    async def stub(**kwargs):
        on_event = kwargs.get("on_event")
        if on_event is not None and "reviewer" in getattr(on_event, "__name__", ""):
            raise RuntimeError("reviewer 子进程崩了")
        if "执行发布" in kwargs.get("prompt", ""):
            text = "已 push。\n\nMR_URL: https://gitlab.example/g/r/-/merge_requests/15\n"
        elif perm_mode(kwargs) == "plan":
            text = "SLUG: add-w\nSTATUS: READY\n"
        else:
            text = "已 commit。\n\nSTATUS: READY\n"
        return fake_result(kwargs, text)

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加功能", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["code_review_rounds"] == 0
    assert row["mr_url"] == "https://gitlab.example/g/r/-/merge_requests/15"


# ---------- 单需求级 review 覆盖（review_override） ----------


def _repo_with_reviewer(base: RepoConfig, *, enabled: bool, max_rounds: int = 1) -> RepoConfig:
    return RepoConfig(
        name=base.name,
        path=base.path,
        default_branch=base.default_branch,
        reviewer=ReviewerConfig(enabled=enabled, max_rounds=max_rounds),
    )


async def _make_session_with_override(db, cfg, repo_cfg, reply, override):
    """建一个落好 review_override 的 session（不 drive），用于检查生效判定。"""
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply)
    await s.create_row(
        initial_request="x", chatid="c", userid="u", review_override=override
    )
    return s


@pytest.mark.parametrize(
    "override, repo_enabled, expect_plan, expect_code",
    [
        (None, True, True, True),    # 不覆盖 + repo 开 → 跟随 repo（开）
        (None, False, False, False),  # 不覆盖 + repo 关 → 跟随 repo（关）
        (True, False, True, True),   # 强制开 + repo 关 → 开（覆盖）
        (False, True, False, False),  # 强制关 + repo 开 → 关（覆盖）
    ],
)
async def test_review_override_matrix_local(
    db, cfg, reply, override, repo_enabled, expect_plan, expect_code
):
    repo_cfg = _repo_with_reviewer(cfg.repos[0], enabled=repo_enabled)
    s = await _make_session_with_override(db, cfg, repo_cfg, reply, override)
    assert s._plan_review_enabled() is expect_plan
    assert s._code_review_enabled() is expect_code


async def test_review_override_remote_enables_code_review(db, cfg, tmp_path, reply):
    """强制开启 + remote 模式：plan 与 code 审查都启用。

    （defer-push 改造后 remote 也支持代码审查；此前 remote 的 code 审查恒被跳过。）
    """
    base = _make_remote_repo_cfg(tmp_path)
    repo_cfg = RepoConfig(
        name=base.name,
        path=base.path,
        default_branch=base.default_branch,
        mode="remote",
        remote_ssh_alias=base.remote_ssh_alias,
        remote_repo_path=base.remote_repo_path,
        remote_worktree_root=base.remote_worktree_root,
        reviewer=ReviewerConfig(enabled=False, max_rounds=1),
    )
    s = await _make_session_with_override(db, cfg, repo_cfg, reply, True)
    assert s._plan_review_enabled() is True
    assert s._code_review_enabled() is True


async def test_review_override_force_on_with_repo_max_rounds_zero(db, cfg, reply):
    """repo 把 max_rounds 显式设 0（等价关）时，单需求强制开至少跑 1 轮，不被架空。"""
    repo_cfg = _repo_with_reviewer(cfg.repos[0], enabled=True, max_rounds=0)
    # 不覆盖：max_rounds=0 → 0<0 为假 → 不审
    s_follow = await _make_session_with_override(db, cfg, repo_cfg, reply, None)
    assert s_follow._plan_review_enabled() is False
    # 强制开：至少 1 轮 → 0<1 → 审
    s_force = await _make_session_with_override(db, cfg, repo_cfg, reply, True)
    assert s_force._plan_review_enabled() is True


async def test_review_override_respects_exhausted_rounds(db, cfg, reply):
    """轮次用尽后即便强制开也不再审，避免来回死循环。"""
    repo_cfg = _repo_with_reviewer(cfg.repos[0], enabled=False, max_rounds=1)
    s = await _make_session_with_override(db, cfg, repo_cfg, reply, True)
    s.row["plan_review_rounds"] = 1  # 已审满 1 轮
    assert s._plan_review_enabled() is False


@pytest.mark.parametrize(
    "override, expected_col",
    [(None, None), (True, 1), (False, 0)],
)
async def test_review_override_persisted_to_db(db, cfg, repo_cfg, reply, override, expected_col):
    """create_row 把 bool|None 覆盖落成 SQLite 的 NULL/1/0。"""
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply)
    await s.create_row(initial_request="x", chatid="c", userid="u", review_override=override)
    row = await db.get_session(s.slug)
    assert row["review_override"] == expected_col


async def test_review_override_on_drives_review_when_repo_default_off(db, cfg, reply, replies):
    """repo 默认关 + 需求 [review] 强制开 → 真的跑了 Reviewer 审查。"""
    repo_cfg = cfg.repos[0]  # 默认 reviewer.enabled=False
    stub = make_role_aware_stub(
        plan_texts=["SLUG: foo-bar\nSTATUS: READY\n"],
        review_texts=["plan 没问题。\n\nREVIEW_VERDICT: APPROVED\n"],
        dev_texts=["完成 ✅\n"],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.create_row(initial_request="加功能", chatid="c1", userid="u1", review_override=True)
    await s.drive()

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert any(c["kind"] == "review" for c in stub.calls)
    assert row["reviewer_session_id"]


async def test_review_override_off_skips_review_when_repo_default_on(db, cfg, reply, replies):
    """repo 默认开 + 需求 [review:off] 强制关 → 不跑任何 Reviewer 审查。"""
    repo_cfg = _reviewer_repo_cfg(cfg.repos[0])  # reviewer.enabled=True
    stub = make_role_aware_stub(
        plan_texts=["SLUG: foo-bar\nSTATUS: READY\n"],
        review_texts=["不该被调用\n\nREVIEW_VERDICT: NEEDS_REVISION\n"],
        dev_texts=["完成 ✅\n"],
    )
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.create_row(initial_request="加功能", chatid="c1", userid="u1", review_override=False)
    await s.drive()

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert all(c["kind"] != "review" for c in stub.calls)
    assert row["reviewer_session_id"] is None


# ---- Reviewer 因上下文过长失败：跳过通知点明根因 ----

async def test_plan_review_skip_reports_length_reason(db, cfg, reply, replies):
    """plan/code 审查因「上下文过长」失败 → 跳过、不拖垮 session，且通知点明根因 +
    处置建议，而非笼统的「审查未完成」。回归 stdin 改造后长度类失败的明确提示。"""
    repo_cfg = _reviewer_repo_cfg(cfg.repos[0])
    too_long = "prompt is too long: 215000 tokens > 200000 maximum"

    async def stub(**kwargs):
        on_event = kwargs.get("on_event")
        if on_event is not None and "reviewer" in getattr(on_event, "__name__", ""):
            # Reviewer 调用：模拟上下文超长失败
            return fake_result(kwargs, "", result_is_error=True, error_message=too_long)
        text = (
            "初版 plan。\n\nSLUG: foo-bar\nSTATUS: READY\n"
            if perm_mode(kwargs) == "plan"
            else "开发完成 ✅\n\nSTATUS: READY\n"
        )
        return fake_result(kwargs, text)

    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="加个功能", chatid="c1", userid="u1")

    # Reviewer 两道关都跳过，但 session 照常走到 COMPLETED（fail-open）
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    # 关键：跳过通知点明根因（上下文过长）+「未经独立审查」，而非笼统「审查未完成」
    texts = [t for _, t in replies]
    assert any("审查跳过" in t and "上下文过长" in t for t in texts)
    assert any("未经独立审查" in t for t in texts)


# ---- worktree 路径自动推导 ----
# 约定：worktree 根目录 = <repo.path>-worktrees


async def test_worktree_auto_derived_from_repo_path(
    db, cfg, reply, replies, tmp_path,
):
    """worktree 自动建在 <repo.path>-worktrees/<slug> 下，
    session 元数据（plan.md 等）仍留在 workspace_root/sessions/<slug>/ 下。"""
    repo_src = tmp_path / "repo-src"
    repo_src.mkdir()
    (repo_src / ".git").mkdir()  # fake repo

    rc = RepoConfig(name="auto-wt", path=repo_src, default_branch="main")

    claude_stub = make_claude_stub([
        "plan 完成。\n\nSLUG: auto-wt-test\nSTATUS: READY\n",
        "开发完成 ✅\n\nSTATUS: READY\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=rc, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    # worktree 应在 <repo.path>-worktrees/<slug> 下
    expected_wt = tmp_path / "repo-src-worktrees" / s.slug
    assert row["worktree_path"] == str(expected_wt)
    assert expected_wt.is_dir()  # fake_create_worktree 已创建

    # session 元数据目录仍在 workspace_root 下
    session_dir = cfg.workspace_root / "sessions" / s.slug
    assert session_dir.is_dir()
    assert (session_dir / "plan.md").exists()

    # worktree 路径不应出现在 session 目录内部
    assert not str(expected_wt).startswith(str(session_dir))


async def test_worktree_auto_derived_from_fixture_repo(
    db, cfg, repo_cfg, reply, replies,
):
    """使用 cfg fixture 的 repo（路径为 tmp_path/repo），验证自动推导为
    tmp_path/repo-worktrees/<slug>。"""
    claude_stub = make_claude_stub([
        "plan 完成。\n\nSLUG: fixture-wt\nSTATUS: READY\n",
        "开发完成 ✅\n\nSTATUS: READY\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    expected_wt = repo_cfg.path.with_name(repo_cfg.path.name + "-worktrees") / s.slug
    assert row["worktree_path"] == str(expected_wt)


async def test_resume_session_with_auto_derived_worktree(
    db, cfg, reply, replies, tmp_path,
):
    """自动推导的 worktree 路径下 session 可正常 resume。"""
    repo_src = tmp_path / "repo-src"
    repo_src.mkdir()
    (repo_src / ".git").mkdir()

    rc = RepoConfig(name="resume-auto", path=repo_src, default_branch="main")

    claude_stub = make_claude_stub([
        "plan 完成。\n\nSLUG: resume-auto-test\nSTATUS: READY\n",
        "开发完成 ✅\n\nSTATUS: READY\n",
        "再次开发 ✅\n\nSTATUS: READY\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=rc, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value

    wt_path = Path(row["worktree_path"])
    expected_wt = tmp_path / "repo-src-worktrees" / s.slug
    assert wt_path == expected_wt
    assert wt_path.is_dir()

    # resume 同一 session
    s2 = Session(db=db, config=cfg, repo_cfg=rc, reply=reply, claude_run=claude_stub)
    await s2.resume(s.slug)

    session_dir = cfg.workspace_root / "sessions" / s.slug
    assert session_dir.is_dir()


# ---- dev 完成正向信号（STATUS: READY）与「疑似未完成」挂起 ----


async def test_dev_ahead_without_ready_parks(db, cfg, repo_cfg, reply, replies):
    """回归线上 bug：dev 提交了部分成果、但以自然语言「还差 X…还是继续推进剩余部分？」
    收尾且未输出 STATUS: READY → 不应误判完成，而应挂起（clarify_phase=dev_confirm）
    等用户确认。"""
    claude_stub = make_claude_stub([
        "plan ok\n\nSLUG: add-x\nSTATUS: READY\n",
        # dev：提交了部分成果，但自然语言提问收尾，没有 STATUS: READY
        "已暂停。A 模块已落盘并 commit。还差 B、C 模块与文档。还是继续推进剩余部分？",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=claude_stub)
    await s.start(initial_request="做个 x", chatid="c1", userid="u1")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value
    assert row["clarify_phase"] == "dev_confirm"
    # 不是澄清，不该占用澄清轮数
    assert row["clarify_rounds"] == 0
    # 既不是完成也不是失败，也没建 MR
    assert row["mr_url"] is None
    # 挂起通知点明「未声明完成」并给「继续 / 完成」恢复入口
    assert any("未声明完成" in t or "疑似" in t for _, t in replies)
    assert any("继续" in t and "完成" in t for _, t in replies)


async def test_dev_ready_and_ahead_completes(db, cfg, repo_cfg, reply, replies):
    """dev 输出 STATUS: READY + 有 commit → 正常 COMPLETED（完成正向信号打通）。"""
    stub = make_claude_stub([
        "plan ok\n\nSLUG: add-x\nSTATUS: READY\n",
        "开发完成并 commit。\n\nSTATUS: READY\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="做个 x", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["mr_url"].endswith("/42")


async def test_dev_ready_but_no_commit_fails(db, cfg, repo_cfg, reply, replies, monkeypatch):
    """声明 STATUS: READY 却无任何 commit → FAIL（无提交可发；不因声明完成就放行空 MR）。"""
    async def fake_no_commits(_path, _base):
        return False
    monkeypatch.setattr(repo_module, "has_commits_ahead", fake_no_commits)

    stub = make_claude_stub([
        "plan ok\n\nSLUG: add-x\nSTATUS: READY\n",
        "我声明完成了但其实没 commit。\n\nSTATUS: READY\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="做个 x", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.FAILED.value
    assert "无新 commit" in (row["last_error"] or "")


async def test_dev_park_then_continue_completes(db, cfg, repo_cfg, reply, replies):
    """park →「继续」→ 重跑 dev、这次声明 STATUS: READY → COMPLETED；
    注入 prompt 用 dev-confirm 专属措辞（区别于澄清回路「待确认问题」）。"""
    stub, calls = make_recording_stub([
        "plan ok\n\nSLUG: add-x\nSTATUS: READY\n",
        "提交了 A，还差 B，继续吗？",                # dev 首轮：有 commit、无 READY → PARK
        "B 也做完了并 commit。\n\nSTATUS: READY\n",   # dev 二轮：完成
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="做个 x", chatid="c1", userid="u1")
    assert (await db.get_session(s.slug))["clarify_phase"] == "dev_confirm"

    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    await s2.handle_user_clarification("继续，把 B 补上", quote_text="[session: add-x]")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    dev2_prompt = calls[2]["prompt"]
    assert "没有声明完成" in dev2_prompt
    assert "继续，把 B 补上" in dev2_prompt


async def test_dev_park_continue_no_new_commit_still_completes(db, cfg, repo_cfg, reply, replies):
    """恢复不死循环关键：park →「继续」→ 模型确认已全部做完、本轮无新 commit（head_sha 不变），
    只补 STATUS: READY → 仍 COMPLETE（commit-delta 不参与判定，不会因「本轮无新增」再挂起）。"""
    stub, calls = make_recording_stub([
        "plan ok\n\nSLUG: add-x\nSTATUS: READY\n",
        "提交了全部改动，但忘了声明完成。",              # dev 首轮：有 commit、无 READY → PARK
        "确认已全部做完，无需再改。\n\nSTATUS: READY\n",  # dev 二轮：只补 READY（fixture 下 head_sha 恒定 → 无新 commit）
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="做个 x", chatid="c1", userid="u1")
    assert (await db.get_session(s.slug))["clarify_phase"] == "dev_confirm"

    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    await s2.handle_user_clarification("继续确认一下", quote_text="[session: add-x]")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value


async def test_dev_park_then_complete_forces_mr(db, cfg, repo_cfg, reply, replies):
    """park →「完成」强制路径：绕过再跑 agent，直接建 MR → COMPLETED；claude 不再被调用。"""
    stub, calls = make_recording_stub([
        "plan ok\n\nSLUG: add-x\nSTATUS: READY\n",
        "先把 A 提交了，还差 B。要不要继续？",   # dev：有 commit、无 READY → PARK
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="做个 x", chatid="c1", userid="u1")
    assert (await db.get_session(s.slug))["state"] == SessionState.AWAITING_USER_CLARIFICATION.value
    n_before = len(calls)  # plan + dev = 2

    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    await s2.handle_user_clarification("完成", quote_text="[session: add-x]")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["mr_url"].endswith("/42")
    # 强制路径不再跑 agent（claude 调用次数不变）
    assert len(calls) == n_before


async def test_dev_confirm_does_not_consume_clarify_rounds(db, cfg, repo_cfg, reply, replies):
    """dev-confirm 挂起不是澄清，多次「继续」都不累加 clarify_rounds、不触发 max 上限 FAIL。"""
    stub = make_claude_stub([
        "plan ok\n\nSLUG: add-x\nSTATUS: READY\n",
        "提交了 A，还差 B。",              # dev 1：PARK
        "提交了 B，还差 C。",              # dev 2（继续后）：又 PARK
        "全做完了。\n\nSTATUS: READY\n",   # dev 3（再继续）：完成
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="做个 x", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value
    assert row["clarify_rounds"] == 0

    for _ in range(2):  # 连续两轮「继续」（cfg.max_clarify_rounds=2，若被累加会误 FAIL）
        s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
        await s2.resume(s.slug)
        await s2.handle_user_clarification("继续")

    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["clarify_rounds"] == 0


async def test_dev_complete_intent_negation_treated_as_continue(db, cfg, repo_cfg, reply, replies):
    """意图匹配保守偏向继续：「还没完成，继续」含否定/继续词 → 判「继续」重跑 dev，不误 ship。"""
    stub, calls = make_recording_stub([
        "plan ok\n\nSLUG: add-x\nSTATUS: READY\n",
        "提交了 A，还差 B。",             # dev1：PARK
        "B 补完了。\n\nSTATUS: READY\n",  # dev2：完成
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="做个 x", chatid="c1", userid="u1")
    n_before = len(calls)  # 2

    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    await s2.handle_user_clarification("还没完成，继续", quote_text="[session: add-x]")

    row = await db.get_session(s.slug)
    # 判「继续」→ 重跑了一轮 dev（call 数 +1），而非强制建 MR
    assert len(calls) == n_before + 1
    assert row["state"] == SessionState.COMPLETED.value


async def test_dev_park_remote_symmetry(db, cfg, tmp_path, reply, replies):
    """remote 对称：dev 无 READY + 远端有 commit → PARK；「完成」→ publish → COMPLETED。"""
    repo_cfg = _make_remote_repo_cfg(tmp_path)
    stub = make_claude_stub([
        "plan ready\n\nSLUG: add-remote-x\nSTATUS: READY\n",
        "在远端提交了部分改动，还没完全做完。",   # remote dev：无 READY → PARK
        # publish（「完成」强制路径 reviewer off → MR_SUBMITTING → _do_publish_remote）
        "已 push 并建 MR。\n\nMR_URL: https://gitlab.example/g/r/-/merge_requests/88\n",
    ])
    s = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s.start(initial_request="远端做个 x", chatid="c1", userid="u1")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value
    assert row["clarify_phase"] == "dev_confirm"

    s2 = Session(db=db, config=cfg, repo_cfg=repo_cfg, reply=reply, claude_run=stub)
    await s2.resume(s.slug)
    await s2.handle_user_clarification("完成", quote_text="[session: add-remote-x]")
    row = await db.get_session(s.slug)
    assert row["state"] == SessionState.COMPLETED.value
    assert row["mr_url"].endswith("/merge_requests/88")
