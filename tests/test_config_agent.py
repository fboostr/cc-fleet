"""配置层 AgentTool / RepoConfig.agent / ReviewerConfig.tool 的最小验证（P1 新增）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_fleet.config.schema import AgentTool, AppConfig, RepoConfig, ReviewerConfig


def test_repo_default_agent_is_claude(tmp_path: Path):
    """不写 agent 字段时默认 claude，旧配置零感知、向后兼容。"""
    rc = RepoConfig(name="x", path=tmp_path)
    assert rc.agent is AgentTool.CLAUDE


def test_repo_agent_parses_from_string(tmp_path: Path):
    """str Enum：配置里写字符串 "claude" 能解析成枚举。"""
    rc = RepoConfig(name="x", path=tmp_path, agent="claude")
    assert rc.agent is AgentTool.CLAUDE


def test_reviewer_tool_defaults_none():
    """ReviewerConfig.tool 预留字段默认 None（= 跟随 repo.agent），P1 不接线。"""
    assert ReviewerConfig().tool is None


def test_reviewer_tool_parses_from_string(tmp_path: Path):
    rc = RepoConfig(
        name="x", path=tmp_path, reviewer=ReviewerConfig(enabled=True, tool="claude")
    )
    assert rc.reviewer.tool is AgentTool.CLAUDE


def test_agent_tool_enum_has_codex_and_opencode():
    """枚举插槽已开：codex / opencode 可从字符串解析（runner 接入与否由 validate_runtime 把关）。"""
    assert AgentTool("codex") is AgentTool.CODEX
    assert AgentTool("opencode") is AgentTool.OPENCODE


def test_agent_config_maps_tool_to_block(tmp_path: Path):
    """AppConfig.agent_config 按工具取对应配置块，默认 binary 与工具同名。"""
    cfg = AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom={"bot_id": "x", "bot_secret": "y"},
        repos=[{"name": "x", "path": str(tmp_path)}],
    )
    assert cfg.agent_config(AgentTool.CLAUDE) is cfg.claude
    assert cfg.agent_config(AgentTool.CODEX) is cfg.codex
    assert cfg.agent_config(AgentTool.OPENCODE) is cfg.opencode
    assert cfg.codex.binary == "codex" and cfg.codex.model is None
    assert cfg.opencode.binary == "opencode" and cfg.opencode.model is None


def test_migrated_timeout_in_claude_section_raises(tmp_path: Path):
    """阶段超时 / 澄清轮次已迁到 pipeline 段；旧 claude 段残留这些字段时显式报错（不静默失效）。"""
    raw = {
        "workspace_root": str(tmp_path / "ws"),
        "log_dir": str(tmp_path / "logs"),
        "db_path": str(tmp_path / "state.db"),
        "wecom": {"bot_id": "x", "bot_secret": "y"},
        "claude": {"binary": "claude", "plan_timeout_sec": 99},
        "repos": [{"name": "x", "path": str(tmp_path)}],
    }
    with pytest.raises(ValueError, match="pipeline"):
        AppConfig.model_validate(raw)
