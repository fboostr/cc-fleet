"""个人微信（ilink ClawBot）接入层。

- ``ilink_client``：腾讯官方 ilink 协议的薄 HTTP 客户端（扫码登录 / 长轮询 / 发消息 / typing）
- ``runner``：``WechatBotRunner``，把 ilink 适配到 cc-fleet 的 ``BotRunner`` 抽象
"""

from .ilink_client import IlinkClient, IlinkMessage
from .runner import WechatBotRunner

__all__ = ["IlinkClient", "IlinkMessage", "WechatBotRunner"]
