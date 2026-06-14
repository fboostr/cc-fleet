"""BotRunner ABC 合约与 create_bot 工厂函数的测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cc_fleet.bot.base import BotRunner
from cc_fleet.bot.factory import create_bot
from cc_fleet.bot.wecom import WecomBotRunner
from cc_fleet.bot.wechat import WechatBotRunner
from cc_fleet.config.schema import (
    AppConfig,
    ClaudeConfig,
    LimitsConfig,
    PlatformType,
    RepoConfig,
    WechatConfig,
    WecomConfig,
)


def _make_config(**overrides) -> AppConfig:
    """构造最小有效配置，允许按需覆盖。"""
    repo = Path("/tmp/my-project")
    base = {
        "workspace_root": Path("/tmp/ws"),
        "log_dir": Path("/tmp/logs"),
        "db_path": Path("/tmp/state.db"),
        "platform": PlatformType.WECOM,
        "wecom": WecomConfig(bot_id="x", bot_secret="y"),
        "repos": [
            RepoConfig(
                name="my-project",
                aliases=["myproj"],
                path=repo,
                default_branch="main",
                keywords=["my-project"],
            ),
        ],
    }
    base.update(overrides)
    return AppConfig(**base)


async def _noop(msg) -> None:
    pass


# ── BotRunner ABC 合约 ──────────────────────────────────────


def test_abc_cannot_instantiate():
    """直接实例化 BotRunner 应抛出 TypeError（abstract methods 未实现）。"""
    with pytest.raises(TypeError):
        BotRunner(on_message=_noop)  # type: ignore[abstract]


# ── create_bot 工厂函数 ─────────────────────────────────────


class _MinimalBot(BotRunner):
    """只实现 abstract method 的最小化子类，用于 ABC 实现验证。"""

    async def reply(self, chatid: str, text: str) -> None:
        pass

    async def run_forever(self) -> None:
        pass


def test_concrete_subclass_can_instantiate():
    """实现了 abstract methods 的子类可以正常实例化。"""
    bot = _MinimalBot(on_message=_noop)
    assert isinstance(bot, BotRunner)


def test_factory_returns_wecom_runner():
    """platform=wecom + 有效 wecom 配置 → 返回 WecomBotRunner 实例。"""
    config = _make_config()
    runner = create_bot(config, on_message=_noop)
    assert isinstance(runner, WecomBotRunner)


def test_factory_raises_on_unsupported_platform():
    """不支持的 platform 值 → ValueError。"""
    config = _make_config()
    # 直接绕过 pydantic 校验设置非法 platform 值
    config.platform = "unsupported"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="不支持的聊天平台"):
        create_bot(config, on_message=_noop)


def test_factory_returns_wechat_runner():
    """platform=wechat + 有效 wechat 配置 → 返回 WechatBotRunner 实例。"""
    config = _make_config(
        platform=PlatformType.WECHAT,
        wecom=None,
        wechat=WechatConfig(bot_token="tok"),
    )
    runner = create_bot(config, on_message=_noop)
    assert isinstance(runner, WechatBotRunner)


# ── 配置 Schema 向后兼容 ────────────────────────────────────


def test_default_platform_is_wecom():
    """不写 platform 字段 → 默认 WECOM（向后兼容旧配置）。"""
    repo = Path("/tmp/my-project")
    cfg = AppConfig(
        workspace_root=Path("/tmp/ws"),
        log_dir=Path("/tmp/logs"),
        db_path=Path("/tmp/state.db"),
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        repos=[
            RepoConfig(
                name="my-project",
                path=repo,
                default_branch="main",
            ),
        ],
    )
    assert cfg.platform == PlatformType.WECOM


def test_platform_wecom_requires_wecom_config():
    """platform=wecom 但 wecom=None → pydantic ValidationError。"""
    repo = Path("/tmp/my-project")
    with pytest.raises(ValidationError, match="wecom"):
        AppConfig(
            workspace_root=Path("/tmp/ws"),
            log_dir=Path("/tmp/logs"),
            db_path=Path("/tmp/state.db"),
            platform=PlatformType.WECOM,
            wecom=None,
            repos=[
                RepoConfig(
                    name="my-project",
                    path=repo,
                    default_branch="main",
                ),
            ],
        )


def test_platform_wechat_requires_wechat_config():
    """platform=wechat 但 wechat=None → pydantic ValidationError。"""
    repo = Path("/tmp/my-project")
    with pytest.raises(ValidationError, match="wechat"):
        AppConfig(
            workspace_root=Path("/tmp/ws"),
            log_dir=Path("/tmp/logs"),
            db_path=Path("/tmp/state.db"),
            platform=PlatformType.WECHAT,
            wechat=None,
            repos=[
                RepoConfig(
                    name="my-project",
                    path=repo,
                    default_branch="main",
                ),
            ],
        )


def test_wecom_config_present_without_explicit_platform():
    """旧格式配置（有 wecom 段、无 platform 字段）直接通过。"""
    repo = Path("/tmp/my-project")
    cfg = AppConfig(
        workspace_root=Path("/tmp/ws"),
        log_dir=Path("/tmp/logs"),
        db_path=Path("/tmp/state.db"),
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        repos=[
            RepoConfig(
                name="my-project",
                path=repo,
                default_branch="main",
            ),
        ],
    )
    assert cfg.platform == PlatformType.WECOM
    assert cfg.wecom is not None
    assert cfg.wecom.bot_id == "x"
