"""多工具横切层：session-id 分配/捕获、agent_tool 落库与恢复钉工具（PR-A 新增）。

覆盖三件事：
- claude（分配式）：_do_new 预生成 UUID，首跑回写恒等、no-op；
- 捕获式工具（以 agent=codex 的行模拟，runner 用测试 stub 注入）：_do_new 不预生成，
  首跑成功后把工具分配的 session id 回写落库；
- agent_tool 钉在行上：create_row 落值；resume 时行内工具与当前配置不一致则按行内
  工具重建工厂解析的 runner（显式注入的 stub 不受影响）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cc_fleet.config.schema import (
    AgentTool,
    AppConfig,
    ClaudeConfig,
    LimitsConfig,
    RepoConfig,
    WecomConfig,
)
from cc_fleet.core import repo as repo_module
from cc_fleet.core import session as session_mod
from cc_fleet.core.session import Session
from cc_fleet.core.state import SessionState
from cc_fleet.storage.db import Database

from tests.conftest import FakeRunner, fake_result

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# plan 阶段停在澄清挂起，drive 只跑 NEW→PLANNING 两步，无需 stub dev/mr 链路
_PLAN_CLARIFY = "需要确认边界。\n\nSLUG: demo-task\nSTATUS: NEED_CLARIFICATION\n"


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.db")
    await d.connect()
    yield d
    await d.close()


def _cfg(tmp_path: Path, *, agent: AgentTool = AgentTool.CLAUDE) -> AppConfig:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        repos=[RepoConfig(name="demo", path=repo_root, agent=agent)],
        limits=LimitsConfig(),
    )


@pytest.fixture(autouse=True)
def stub_git(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(_root: Path, _branch: str, _remote: str = "origin") -> None:
        return None

    async def fake_create_worktree(_root: Path, path: Path, _branch: str, _base: str) -> None:
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(repo_module, "fetch_default_branch", fake_fetch)
    monkeypatch.setattr(repo_module, "create_worktree", fake_create_worktree)


async def _reply(_chatid: str, _text: str) -> None:
    return None


def _recording_stub(calls: list[dict], *, session_id_override: str | None = None):
    async def stub(**kwargs):
        calls.append(kwargs)
        if session_id_override is not None:
            return fake_result(kwargs, _PLAN_CLARIFY, session_id=session_id_override)
        return fake_result(kwargs, _PLAN_CLARIFY)

    return stub


# ---- session-id：分配 vs 捕获 ----

async def test_claude_preassigns_uuid_and_writeback_is_noop(db, tmp_path):
    cfg = _cfg(tmp_path)
    calls: list[dict] = []
    session = Session(
        db=db, config=cfg, repo_cfg=cfg.repos[0], reply=_reply,
        claude_run=_recording_stub(calls),
    )
    await session.start(initial_request="加个排序", chatid="c", userid="u")

    row = await db.get_session(session.slug)
    assert row["state"] == SessionState.AWAITING_USER_CLARIFICATION.value
    # 分配式：进 plan 前已预生成 UUID，且首跑回写不改变它
    assert _UUID_RE.match(row["claude_session_id"] or "")
    assert calls[0]["session_id"] == row["claude_session_id"]
    assert calls[0]["resume_from"] is None


async def test_capture_style_tool_writes_back_session_id(db, tmp_path):
    """agent=codex 的行不预生成 sid；首跑把工具分配的 id（可含大写）回写落库。"""
    cfg = _cfg(tmp_path, agent=AgentTool.CODEX)
    calls: list[dict] = []
    session = Session(
        db=db, config=cfg, repo_cfg=cfg.repos[0], reply=_reply,
        claude_run=_recording_stub(calls, session_id_override="Sess_ABC123xyz"),
    )
    await session.start(initial_request="加个排序", chatid="c", userid="u")

    row = await db.get_session(session.slug)
    assert row["agent_tool"] == "codex"
    # 捕获式：首跑前行内无 sid（runner 收到空串占位），首跑后落工具分配的 id
    assert calls[0]["session_id"] == ""
    assert calls[0]["resume_from"] is None
    assert row["claude_session_id"] == "Sess_ABC123xyz"


# ---- agent_tool 落库与恢复钉工具 ----

async def test_create_row_pins_agent_tool(db, tmp_path):
    cfg = _cfg(tmp_path)
    session = Session(
        db=db, config=cfg, repo_cfg=cfg.repos[0], reply=_reply,
        claude_run=_recording_stub([]),
    )
    await session.create_row(initial_request="x", chatid="c", userid="u")
    row = await db.get_session(session.slug)
    assert row["agent_tool"] == "claude"


async def test_resume_rebuilds_runner_from_row_agent_tool(db, tmp_path, monkeypatch):
    """行内钉了 claude、当前配置改成 codex：resume 后按行内工具重建 runner。"""
    cfg = _cfg(tmp_path)
    creator = Session(
        db=db, config=cfg, repo_cfg=cfg.repos[0], reply=_reply,
        claude_run=_recording_stub([]),
    )
    await creator.create_row(initial_request="x", chatid="c", userid="u")

    made: list[AgentTool] = []

    def fake_get_runner(tool, _config):
        made.append(tool)
        return FakeRunner(_recording_stub([]))

    monkeypatch.setattr(session_mod, "get_runner", fake_get_runner)
    cfg2 = _cfg(tmp_path, agent=AgentTool.CODEX)
    resumer = Session(db=db, config=cfg2, repo_cfg=cfg2.repos[0], reply=_reply)
    assert made == [AgentTool.CODEX, AgentTool.CODEX]  # __init__：coder + reviewer 各一次

    await resumer.resume(creator.slug)
    # 行内 agent_tool=claude ≠ 配置 codex → coder 与「跟随 coder」的 reviewer 都按行内重建
    assert made[2:] == [AgentTool.CLAUDE, AgentTool.CLAUDE]


async def test_resume_keeps_injected_stub_runner(db, tmp_path):
    """显式注入（claude_run 测试 stub）的 runner 不被钉工具逻辑重建。"""
    cfg = _cfg(tmp_path)
    creator = Session(
        db=db, config=cfg, repo_cfg=cfg.repos[0], reply=_reply,
        claude_run=_recording_stub([]),
    )
    await creator.create_row(initial_request="x", chatid="c", userid="u")

    cfg2 = _cfg(tmp_path, agent=AgentTool.CODEX)
    resumer = Session(
        db=db, config=cfg2, repo_cfg=cfg2.repos[0], reply=_reply,
        claude_run=_recording_stub([]),
    )
    runner_before = resumer.runner
    await resumer.resume(creator.slug)
    assert resumer.runner is runner_before


async def test_resume_legacy_row_without_agent_tool_follows_config(db, tmp_path, monkeypatch):
    """老数据（agent_tool 为 NULL）回退当前配置，不触发重建。"""
    cfg = _cfg(tmp_path)
    creator = Session(
        db=db, config=cfg, repo_cfg=cfg.repos[0], reply=_reply,
        claude_run=_recording_stub([]),
    )
    await creator.create_row(initial_request="x", chatid="c", userid="u")
    await db.update_session(creator.slug, agent_tool=None)  # 模拟迁移前的老行

    made: list[AgentTool] = []

    def fake_get_runner(tool, _config):
        made.append(tool)
        return FakeRunner(_recording_stub([]))

    monkeypatch.setattr(session_mod, "get_runner", fake_get_runner)
    resumer = Session(db=db, config=cfg, repo_cfg=cfg.repos[0], reply=_reply)
    n_init = len(made)
    await resumer.resume(creator.slug)
    assert len(made) == n_init  # 无重建
