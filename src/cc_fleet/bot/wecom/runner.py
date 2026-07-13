"""企业微信智能机器人接入层（封装 wecom-aibot-python-sdk，适配 cc-fleet 的会话模型）。

- 维护与企微的 WebSocket 长连接（SDK 自带无限重连）
- 把进来的文本消息归一化为 IncomingMessage 投递给上层
- 提供 reply(chatid, text) 主动推送 Markdown
- 内置最小节流：全局最小发送间隔 + 每会话 60s 滑动窗口
"""

from __future__ import annotations

import asyncio
import logging
import time
from aibot import WSClient, WSClientOptions  # type: ignore[import-not-found]

from ..base import BotDeliveryError, BotRunner, OnMessage
from ..message import IncomingMessage

logger = logging.getLogger(__name__)

# 基于企微 aibot 官方文档的节流参数（全局上限 ~20条/分钟、单会话 30/分钟）
_RATE_LIMIT_PER_MIN = 25
_RATE_WINDOW_SEC = 60
_MIN_SEND_INTERVAL = 3.5  # 秒
_SEND_MAX_ATTEMPTS = 3
_SEND_RETRY_BASE_SEC = 1.0


def _extract_quote_text(quote: dict) -> str:
    """解析 body.quote 中的被引用消息文本（text / mixed 两种 msgtype）。"""
    if not quote:
        return ""
    msgtype = quote.get("msgtype", "")
    if msgtype == "text":
        return (quote.get("text", {}) or {}).get("content", "").strip()
    if msgtype == "mixed":
        items = (quote.get("mixed", {}) or {}).get("items", [])
        return " ".join(
            (it.get("text", {}) or {}).get("content", "")
            for it in items
            if it.get("type") == "text"
        ).strip()
    return ""


class WecomBotRunner(BotRunner):
    def __init__(
        self,
        *,
        bot_id: str,
        bot_secret: str,
        allowed_chatids: list[str] | None,
        on_message: OnMessage,
    ) -> None:
        super().__init__(on_message=on_message)
        self._allowed = set(allowed_chatids or [])
        self._ws = WSClient(
            WSClientOptions(
                bot_id=bot_id,
                secret=bot_secret,
                max_reconnect_attempts=-1,
            )
        )
        self._send_timestamps: dict[str, list[float]] = {}
        self._last_send_time: float = 0.0
        self._send_lock = asyncio.Lock()
        self._setup_handlers()

    # ---------- SDK 事件 ----------

    def _setup_handlers(self) -> None:
        @self._ws.on("authenticated")
        def _on_auth() -> None:  # noqa: ARG001
            logger.info("企微机器人认证成功")

        @self._ws.on("disconnected")
        def _on_disconnect(reason: str) -> None:  # noqa: ARG001
            logger.warning("企微连接断开：%s", reason)

        @self._ws.on("error")
        def _on_error(err: Exception) -> None:  # noqa: ARG001
            logger.error("企微连接异常：%s", err)

        @self._ws.on("reconnecting")
        def _on_reconnecting(attempt: int) -> None:  # noqa: ARG001
            logger.info("企微正在重连（第 %d 次）", attempt)

        @self._ws.on("message.text")
        async def _on_text(frame: dict) -> None:
            try:
                await self._dispatch(frame)
            except Exception:  # noqa: BLE001
                logger.exception("处理用户消息时未捕获异常")

    async def _dispatch(self, frame: dict) -> None:
        body = frame.get("body", {}) or {}
        chatid = body.get("chatid", "") or frame.get("chatid", "") or ""
        from_info = body.get("from", {}) or frame.get("from", {}) or {}
        userid = from_info.get("userid", "") if isinstance(from_info, dict) else ""

        if self._allowed and chatid and chatid not in self._allowed:
            logger.info("忽略不在白名单的 chatid=%s", chatid)
            return

        text = ((body.get("text", {}) or {}).get("content", "") or "").strip()
        if not text:
            return

        quote_text = _extract_quote_text(body.get("quote", {}) or {})

        msg = IncomingMessage(
            text=text,
            quote_text=quote_text,
            chatid=chatid,
            userid=userid,
        )
        await self._on_message(msg)

    # ---------- 主动发消息 ----------

    async def _acquire_send_slot(self, chatid: str) -> None:
        while True:
            now = time.monotonic()
            ts = [t for t in self._send_timestamps.get(chatid, []) if now - t < _RATE_WINDOW_SEC]
            if len(ts) < _RATE_LIMIT_PER_MIN:
                ts.append(now)
                self._send_timestamps[chatid] = ts
                return
            wait = _RATE_WINDOW_SEC - (now - ts[0]) + 0.1
            logger.warning(
                "会话 %s 60s 内已发 %d 条，等待 %.1fs", chatid, len(ts), wait
            )
            await asyncio.sleep(wait)

    async def reply(self, chatid: str, text: str) -> None:
        """向指定 chatid 主动发送一条 markdown 消息。"""
        if not chatid:
            raise BotDeliveryError("reply 调用缺少 chatid")
        async with self._send_lock:
            last_error: Exception | None = None
            for attempt in range(_SEND_MAX_ATTEMPTS):
                elapsed = time.monotonic() - self._last_send_time
                if elapsed < _MIN_SEND_INTERVAL:
                    await asyncio.sleep(_MIN_SEND_INTERVAL - elapsed)
                await self._acquire_send_slot(chatid)
                try:
                    await self._ws.send_message(
                        chatid,
                        {"msgtype": "markdown", "markdown": {"content": text}},
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    logger.warning(
                        "发送企微消息失败 chatid=%s attempt=%d/%d：%s",
                        chatid,
                        attempt + 1,
                        _SEND_MAX_ATTEMPTS,
                        exc,
                    )
                    if attempt + 1 < _SEND_MAX_ATTEMPTS:
                        await asyncio.sleep(_SEND_RETRY_BASE_SEC * (2**attempt))
                else:
                    self._last_send_time = time.monotonic()
                    return
            raise BotDeliveryError(
                f"企微消息重试 {_SEND_MAX_ATTEMPTS} 次仍失败：{last_error}"
            ) from last_error

    # ---------- 生命周期 ----------

    async def run_forever(self) -> None:
        await self._ws.connect()
        await asyncio.Event().wait()

    async def shutdown(self) -> None:
        """断开企微 WebSocket 连接。"""
        logger.info("企微机器人断开连接")
        try:
            # aibot WSClient.disconnect() 是同步方法（返回 None），不能 await——
            # 否则会 `TypeError: 'NoneType' object can't be awaited`。
            self._ws.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("企微 WebSocket 断开失败")
