"""个人微信（ilink ClawBot）接入层，适配 cc-fleet 的会话模型。

- 长轮询 ilink ``getupdates`` 收消息，归一化为 ``IncomingMessage`` 投递给上层
- 维护 ``from_user_id`` → 最近一次 ``context_token`` 的映射：ilink 回复强依赖 inbound
  带回的 context_token，而上层 ``reply(chatid, text)`` 只有 chatid（单聊里等于
  from_user_id），故在本层补齐 token（详见 plan 风险 1：token 有效期需联调确认）
- 收到消息即 best-effort 发一次 typing「正在输入」
- 长轮询游标 ``get_updates_buf`` 持久化到文件，避免重启重复/丢消息
- 内置最小节流：全局最小发送间隔 + 每用户 60s 滑动窗口
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .ilink_client import IlinkClient, IlinkMessage
from ..base import BotRunner, OnMessage
from ..message import IncomingMessage

logger = logging.getLogger(__name__)

# ilink 未公布发送限频，取保守值；联调后按实际调整
_RATE_LIMIT_PER_MIN = 20
_RATE_WINDOW_SEC = 60
_MIN_SEND_INTERVAL = 1.0  # 秒

# 单次长轮询异常后的重试退避上限
_MAX_BACKOFF_SEC = 30.0


class WechatBotRunner(BotRunner):
    def __init__(
        self,
        *,
        bot_token: str,
        base_url: str,
        allowed_user_ids: list[str] | None,
        on_message: OnMessage,
        cursor_path: Path,
    ) -> None:
        super().__init__(on_message=on_message)
        self._allowed = set(allowed_user_ids or [])
        self._client = IlinkClient(base_url=base_url, bot_token=bot_token)
        self._cursor_path = Path(cursor_path)
        self._cursor = self._load_cursor()
        # from_user_id → (最近一次 context_token, 捕获时的 monotonic 时刻)。
        # 存捕获时刻是为了在发送时打出 token 年龄，便于判断回复失败是否因 token 过期。
        self._context_tokens: dict[str, tuple[str, float]] = {}
        self._typing_ticket: str | None = None
        self._send_timestamps: dict[str, list[float]] = {}
        self._last_send_time: float = 0.0
        self._send_lock = asyncio.Lock()
        # inbound 收到后投入队列，由单独的 worker 顺序消费，避免发送/开 session/typing
        # 卡住 getupdates 长轮询（详见 run_forever / _worker）。
        self._inbox: asyncio.Queue[IlinkMessage] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._stop = False

    # ---------- 游标持久化 ----------

    def _load_cursor(self) -> str:
        try:
            return self._cursor_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except Exception:  # noqa: BLE001
            logger.warning("读取 ilink 游标失败，从空游标开始", exc_info=True)
            return ""

    def _persist_cursor(self) -> None:
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self._cursor_path.write_text(self._cursor, encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("持久化 ilink 游标失败", exc_info=True)

    # ---------- 收消息 ----------

    async def _handle(self, m: IlinkMessage) -> None:
        try:
            # 完整原始报文，便于排查引用/媒体等结构差异（DEBUG，平时不打）
            logger.debug("ilink inbound 原始报文: %r", m.raw)
            if not m.from_user_id:
                return
            if self._allowed and m.from_user_id not in self._allowed:
                logger.info("忽略不在白名单的 user=%s", m.from_user_id)
                return
            # 即便正文为空也先记下 context_token（图片/文件等非文本消息也带 token）
            if m.context_token:
                self._context_tokens[m.from_user_id] = (m.context_token, time.monotonic())
            else:
                # 某些消息可能不带 token；此时沿用旧 token，回复有失败风险，记下来便于排查
                logger.warning(
                    "inbound from=%s 不带 context_token，沿用旧 token（回复可能失败）",
                    m.from_user_id,
                )
            text = m.text.strip()
            if not text:
                return
            await self._maybe_send_typing(m.from_user_id)
            msg = IncomingMessage(
                text=text,
                quote_text=m.quote_text,  # 被引用消息文本（含 [session: <slug>]，供续聊/指令反解）
                chatid="",                # 个人微信单聊，无群 chatid
                userid=m.from_user_id,
            )
            await self._on_message(msg)
        except Exception:  # noqa: BLE001
            logger.exception("处理 ilink 消息失败 from=%s", m.from_user_id)

    async def _maybe_send_typing(self, user_id: str) -> None:
        """best-effort：失败不影响主流程。"""
        try:
            if self._typing_ticket is None:
                cfg = await self._client.get_config()
                self._typing_ticket = cfg.get("typing_ticket") or ""
            if self._typing_ticket:
                await self._client.send_typing(
                    to_user_id=user_id, typing_ticket=self._typing_ticket
                )
        except Exception:  # noqa: BLE001
            logger.debug("发送 typing 失败（忽略）", exc_info=True)

    # ---------- 发消息与节流 ----------

    async def _acquire_send_slot(self, user_id: str) -> None:
        while True:
            now = time.monotonic()
            ts = [
                t
                for t in self._send_timestamps.get(user_id, [])
                if now - t < _RATE_WINDOW_SEC
            ]
            if len(ts) < _RATE_LIMIT_PER_MIN:
                ts.append(now)
                self._send_timestamps[user_id] = ts
                return
            wait = _RATE_WINDOW_SEC - (now - ts[0]) + 0.1
            logger.warning("用户 %s 60s 内已发 %d 条，等待 %.1fs", user_id, len(ts), wait)
            await asyncio.sleep(wait)

    async def reply(self, chatid: str, text: str) -> None:
        """向指定用户（chatid==from_user_id）发一条文本消息。

        ilink 回复需带 inbound 的 context_token；找不到则告警跳过（不崩）。
        发送失败（含 ret!=0 的 IlinkError）只记日志、不向上抛，避免拖垮消费 worker。
        """
        if not chatid:
            logger.warning("reply 缺少目标 user_id，忽略")
            return
        entry = self._context_tokens.get(chatid)
        if not entry:
            logger.warning(
                "找不到 user=%s 的 context_token，无法回复"
                "（进程重启丢失，或从未收到该用户消息）",
                chatid,
            )
            return
        token, captured = entry
        age = time.monotonic() - captured
        logger.info(
            "reply user=%s token=…%s age=%.0fs text=%.40s",
            chatid, token[-6:], age, text.replace("\n", " "),
        )
        async with self._send_lock:
            elapsed = time.monotonic() - self._last_send_time
            if elapsed < _MIN_SEND_INTERVAL:
                await asyncio.sleep(_MIN_SEND_INTERVAL - elapsed)
            await self._acquire_send_slot(chatid)
            try:
                await self._client.send_message(
                    to_user_id=chatid, context_token=token, text=text
                )
                self._last_send_time = time.monotonic()
                logger.info("reply 发送成功 user=%s", chatid)
            except Exception:  # noqa: BLE001
                # IlinkError 的 __str__ 已含 ret 与响应体；token_age 一并打出便于判断 TTL
                logger.exception(
                    "发送 ilink 消息失败 user=%s token_age=%.0fs", chatid, age
                )

    # ---------- 生命周期 ----------

    async def _worker(self) -> None:
        """顺序消费 inbox 里的消息。

        放到独立协程里，使「发送/开 session/typing 耗时」不会阻塞下面 run_forever 的
        getupdates 长轮询；单 worker 保证处理顺序与收到顺序一致。
        """
        while True:
            m = await self._inbox.get()
            try:
                await self._handle(m)
            finally:
                self._inbox.task_done()

    async def run_forever(self) -> None:
        logger.info("微信(ilink)机器人启动，开始长轮询")
        self._worker_task = asyncio.create_task(self._worker())
        backoff = 1.0
        while not self._stop:
            try:
                msgs, new_buf = await self._client.get_updates(self._cursor)
                backoff = 1.0
                for m in msgs:
                    self._inbox.put_nowait(m)  # 入队不阻塞，立刻回到收消息
                if new_buf != self._cursor:
                    self._cursor = new_buf
                    self._persist_cursor()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("ilink 长轮询异常，%.1fs 后重试", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SEC)

    async def shutdown(self) -> None:
        logger.info("微信(ilink)机器人停止")
        self._stop = True
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        await self._client.close()
