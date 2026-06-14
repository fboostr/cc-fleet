"""聊天平台 Runner 的抽象基类。

每个聊天平台（企微 / Slack / 飞书 / Discord 等）实现一个 ``BotRunner`` 子类，
封装平台的连接生命周期和消息格式。上层业务逻辑只依赖本模块定义的接口，
不感知具体平台。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from .message import IncomingMessage

logger = logging.getLogger(__name__)

OnMessage = Callable[[IncomingMessage], Awaitable[None]]


class BotRunner(ABC):
    """聊天平台 Runner 的最小接口合约。

    子类负责：
    - 平台连接生命周期（WebSocket / HTTP Webhook / Long Polling）
    - 平台特有的消息格式 → ``IncomingMessage`` 的归一化
    - 通过 ``reply(chatid, text)`` 向平台发送富文本回复
    """

    def __init__(self, *, on_message: OnMessage) -> None:
        self._on_message = on_message

    @abstractmethod
    async def reply(self, chatid: str, text: str) -> None:
        """向指定 *chatid* 发送一条消息（内容为平台支持的富文本格式）。"""
        ...

    @abstractmethod
    async def run_forever(self) -> None:
        """建立连接并持续派发消息，阻塞直到进程退出。"""
        ...

    async def shutdown(self) -> None:
        """优雅关闭：断开连接、释放资源。默认空实现。"""
        logger.debug("%s.shutdown 调用（默认空实现）", type(self).__name__)
