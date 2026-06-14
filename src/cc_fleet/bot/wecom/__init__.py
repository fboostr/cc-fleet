"""企业微信（wecom aibot）接入层。

- ``runner``：``WecomBotRunner``，封装 aibot WebSocket SDK 并适配 cc-fleet 的 ``BotRunner`` 抽象
"""

from .runner import WecomBotRunner

__all__ = ["WecomBotRunner"]
