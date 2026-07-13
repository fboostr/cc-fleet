"""runner_factory.get_runner 的分发与错误路径（P1 新增）。"""

from __future__ import annotations

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
from cc_fleet.core.runner_factory import SUPPORTED_TOOLS, get_runner
from cc_fleet.core.runners.claude import ClaudeRunner
from cc_fleet.core.runners.codex import CodexRunner


def _cfg(tmp_path: Path, *, binary: str = "claude") -> AppConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(binary=binary),
        repos=[RepoConfig(name="r", path=repo)],
        limits=LimitsConfig(),
    )


def test_get_runner_claude_returns_claude_runner(tmp_path: Path):
    runner = get_runner(AgentTool.CLAUDE, _cfg(tmp_path, binary="/custom/claude"))
    assert isinstance(runner, ClaudeRunner)
    # binary 取自 config.claude.binary，由 runner 持有
    assert runner._binary == "/custom/claude"


def test_get_runner_unknown_tool_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        get_runner("nope", _cfg(tmp_path))  # type: ignore[arg-type]


def test_get_runner_codex_returns_codex_runner(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.codex.binary = "/custom/codex"
    runner = get_runner(AgentTool.CODEX, cfg)
    assert isinstance(runner, CodexRunner)
    assert runner._binary == "/custom/codex"


def test_get_runner_enum_without_runner_raises(tmp_path: Path):
    """枚举已有但 runner 未接入的工具（opencode）走 ValueError 兜底。"""
    with pytest.raises(ValueError, match="unsupported"):
        get_runner(AgentTool.OPENCODE, _cfg(tmp_path))


def test_supported_tools_claude_and_codex():
    """SUPPORTED_TOOLS 是「runner 已接入」的单一事实源；接入新工具时同步更新此断言。"""
    assert SUPPORTED_TOOLS == frozenset({AgentTool.CLAUDE, AgentTool.CODEX})
