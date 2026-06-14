"""RepoConfig.reviewer / PipelineConfig.review_timeout_sec 的默认值与解析。"""

from __future__ import annotations

from pathlib import Path

from cc_fleet.config.schema import AppConfig, PipelineConfig, RepoConfig, ReviewerConfig


def test_reviewer_defaults_disabled(tmp_path: Path):
    rc = RepoConfig(name="x", path=tmp_path)
    assert rc.reviewer.enabled is False
    assert rc.reviewer.max_rounds == 1


def test_review_timeout_default():
    assert PipelineConfig().review_timeout_sec == 1800


def test_reviewer_parses_from_nested_dict(tmp_path: Path):
    """嵌套 reviewer 配置应能被 pydantic 正确解析。"""
    cfg = AppConfig.model_validate(
        {
            "workspace_root": str(tmp_path / "ws"),
            "log_dir": str(tmp_path / "logs"),
            "db_path": str(tmp_path / "state.db"),
            "wecom": {"bot_id": "x", "bot_secret": "y"},
            "repos": [
                {
                    "name": "demo",
                    "path": str(tmp_path),
                    "reviewer": {"enabled": True, "max_rounds": 2},
                },
                {
                    "name": "plain",
                    "path": str(tmp_path),
                },
            ],
        }
    )
    assert cfg.repos[0].reviewer == ReviewerConfig(enabled=True, max_rounds=2)
    # 未配 reviewer 的 repo 走默认（关闭）
    assert cfg.repos[1].reviewer.enabled is False
