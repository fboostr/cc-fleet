"""聊天平台 Runner 工厂函数。

按配置中的 ``platform`` 字段选择并构造对应的 ``BotRunner`` 实例。
新增平台时只需在此添加一个 ``if`` 分支。各平台 Runner 惰性 import：
只有真正选用某平台时才加载它的依赖（如 wecom 的 ``aibot`` SDK），
避免单平台部署被另一平台的依赖牵连。
"""

from __future__ import annotations

from .base import BotRunner, OnMessage
from ..config.schema import AppConfig, PlatformType


def create_bot(config: AppConfig, *, on_message: OnMessage) -> BotRunner:
    """按 ``config.platform`` 构造对应的 BotRunner 实例。"""
    if config.platform == PlatformType.WECOM:
        assert config.wecom is not None, "model_validator 已确保 platform=wecom 时 wecom 不为 None"
        from .wecom import WecomBotRunner

        return WecomBotRunner(
            bot_id=config.wecom.bot_id,
            bot_secret=config.wecom.bot_secret,
            allowed_chatids=config.wecom.allowed_chatids,
            on_message=on_message,
        )
    if config.platform == PlatformType.WECHAT:
        assert config.wechat is not None, "model_validator 已确保 platform=wechat 时 wechat 不为 None"
        from .wechat import WechatBotRunner

        return WechatBotRunner(
            bot_token=config.wechat.bot_token,
            base_url=config.wechat.base_url,
            allowed_user_ids=config.wechat.allowed_user_ids,
            on_message=on_message,
            cursor_path=config.workspace_root / "wechat_cursor.txt",
            refs_path=config.workspace_root / "wechat_outbound_refs.jsonl",
        )
    raise ValueError(f"不支持的聊天平台：{config.platform}")
