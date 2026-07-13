"""启动期 AppConfig.validate_runtime 静态校验。"""

from __future__ import annotations

from pathlib import Path

from cc_fleet.config.schema import (
    AppConfig,
    ClaudeConfig,
    LimitsConfig,
    RepoConfig,
    WecomConfig,
)


def _make_cfg(tmp_path: Path, repos: list[RepoConfig]) -> AppConfig:
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        repos=repos,
        limits=LimitsConfig(),
    )


def test_local_with_git_passes(tmp_path: Path):
    repo = tmp_path / "with-git"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = _make_cfg(tmp_path, [RepoConfig(name="x", path=repo)])
    assert cfg.validate_runtime() == []


def test_local_without_git_fails_with_helpful_hint(tmp_path: Path):
    repo = tmp_path / "no-git"
    repo.mkdir()
    cfg = _make_cfg(tmp_path, [RepoConfig(name="my-project", path=repo)])
    errs = cfg.validate_runtime()
    assert len(errs) == 1
    assert "my-project" in errs[0]
    assert ".git" in errs[0]
    assert "mode: remote" in errs[0]


def test_local_git_as_file_passes(tmp_path: Path):
    """`.git` 也可以是一个文件（worktree 内部的 git 文件指向主仓库）。"""
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt", encoding="utf-8")
    cfg = _make_cfg(tmp_path, [RepoConfig(name="x", path=repo)])
    assert cfg.validate_runtime() == []


def test_remote_existing_shell_dir_passes(tmp_path: Path):
    shell = tmp_path / "shell"
    shell.mkdir()
    cfg = _make_cfg(
        tmp_path,
        [
            RepoConfig(
                name="r",
                path=shell,
                mode="remote",
                remote_ssh_alias="h",
                remote_repo_path="/p",
                remote_worktree_root="/r",
            )
        ],
    )
    assert cfg.validate_runtime() == []


def test_remote_missing_shell_dir_fails(tmp_path: Path):
    missing = tmp_path / "missing"
    cfg = _make_cfg(
        tmp_path,
        [
            RepoConfig(
                name="r",
                path=missing,
                mode="remote",
                remote_ssh_alias="h",
                remote_repo_path="/p",
                remote_worktree_root="/r",
            )
        ],
    )
    errs = cfg.validate_runtime()
    assert len(errs) == 1 and "不存在" in errs[0]


def test_agent_without_runner_fails(tmp_path: Path):
    """agent 引用枚举已有、runner 未接入的工具（opencode）→ 启动期报错，而非运行到工厂才炸。"""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = _make_cfg(tmp_path, [RepoConfig(name="x", path=repo, agent="opencode")])
    errs = cfg.validate_runtime()
    assert len(errs) == 1
    assert "opencode" in errs[0] and "尚未接入" in errs[0]


def test_agent_codex_passes_with_warning(tmp_path: Path, caplog):
    """codex runner 已接入：配置通过校验（零 error），但启动期 WARN 点明护栏缺口。"""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = _make_cfg(tmp_path, [RepoConfig(name="x", path=repo, agent="codex")])
    import logging

    with caplog.at_level(logging.WARNING, logger="cc_fleet.config.schema"):
        errs = cfg.validate_runtime()
    assert errs == []
    assert any("force-push" in r.message for r in caplog.records)


def test_reviewer_tool_without_runner_fails(tmp_path: Path):
    """reviewer.tool 同样受「runner 已接入」校验。"""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = _make_cfg(
        tmp_path,
        [RepoConfig(name="x", path=repo, reviewer={"enabled": True, "tool": "opencode"})],
    )
    errs = cfg.validate_runtime()
    assert len(errs) == 1
    assert "opencode" in errs[0] and "reviewer.tool" in errs[0]


def test_multiple_repos_collect_all_errors(tmp_path: Path):
    bad1 = tmp_path / "bad1"
    bad1.mkdir()
    bad2 = tmp_path / "bad2"  # 不创建
    cfg = _make_cfg(
        tmp_path,
        [
            RepoConfig(name="a", path=bad1),                              # mode=local 无 .git
            RepoConfig(name="b", path=bad2, mode="remote",
                       remote_ssh_alias="h", remote_repo_path="/p", remote_worktree_root="/r"),  # path 不存在
        ],
    )
    errs = cfg.validate_runtime()
    assert len(errs) == 2
    assert any("a" in e and ".git" in e for e in errs)
    assert any("b" in e and "不存在" in e for e in errs)
