"""IM 接入层与业务层之间的标准化消息结构（跨聊天平台共用）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IncomingMessage:
    """从聊天平台机器人收到的一条用户消息（已归一化，平台无关）。

    - text: 用户消息正文（已剥去平台特有的富文本封装，仅保留纯文本）
    - quote_text: 用户引用的历史消息的原文文本（拼接好的，可能为空字符串）
    - chatid: 群聊 ID；单聊或个人微信等无群概念时为空字符串
    - userid: 发送人在该平台的用户标识
    """

    text: str
    quote_text: str
    chatid: str
    userid: str


@dataclass
class OutgoingReply:
    """需要回发到聊天平台的一条消息。

    主控生成回复时**必须**在内容里包含 `[session: <slug>]` 行，
    以便用户引用回来时可被反向解析。
    """

    chatid: str
    text: str
