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
import json
import logging
import time
from pathlib import Path

from .ilink_client import IlinkClient, IlinkMessage
from ..base import BotRunner, OnMessage
from ..message import IncomingMessage
from ...util.ids import find_session_tag

logger = logging.getLogger(__name__)

# ilink 未公布发送限频，取保守值；联调后按实际调整
_RATE_LIMIT_PER_MIN = 20
_RATE_WINDOW_SEC = 60
_MIN_SEND_INTERVAL = 1.0  # 秒

# 单次长轮询异常后的重试退避上限
_MAX_BACKOFF_SEC = 30.0

# ── 引用消息「时间戳关联」还原 ────────────────────────────────
# ilink 新版引用只回传被引用消息的 create_time_ms（不再内联其文本），该时间戳与我们
# 本地发送该消息的时刻对齐（实测差 <1s）。故发消息时记「发送时刻 → session 标签」，
# 收到引用时用 create_time_ms 按容差反查还原标签。
# 反查容差（ms）：兜住本地时钟与 ilink 服务端时钟的轻微偏差。
_REF_MATCH_TOLERANCE_MS = 5_000
# 出站标签记录的保留窗口与条数上限（用户可能引用较老的消息）。
_OUTBOUND_REF_RETENTION_MS = 30 * 24 * 3600 * 1000
_OUTBOUND_REF_MAX = 5000


class WechatBotRunner(BotRunner):
    def __init__(
        self,
        *,
        bot_token: str,
        base_url: str,
        allowed_user_ids: list[str] | None,
        on_message: OnMessage,
        cursor_path: Path,
        refs_path: Path | None = None,
    ) -> None:
        super().__init__(on_message=on_message)
        self._allowed = set(allowed_user_ids or [])
        self._client = IlinkClient(base_url=base_url, bot_token=bot_token)
        self._cursor_path = Path(cursor_path)
        self._cursor = self._load_cursor()
        # 出站 session 标签记录（发送时刻→标签），用于引用消息按时间戳反查还原。
        self._refs_path = (
            Path(refs_path)
            if refs_path is not None
            else self._cursor_path.parent / "wechat_outbound_refs.jsonl"
        )
        self._outbound_refs: list[tuple[int, str]] = self._load_outbound_refs()
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

    # ---------- 出站标签记录（引用时间戳关联）----------

    def _prune_refs(self, refs: list[tuple[int, str]]) -> list[tuple[int, str]]:
        """按保留窗口与条数上限裁剪出站标签记录（保序，保留较新的）。"""
        if not refs:
            return refs
        cutoff = int(time.time() * 1000) - _OUTBOUND_REF_RETENTION_MS
        refs = [r for r in refs if r[0] >= cutoff]
        if len(refs) > _OUTBOUND_REF_MAX:
            refs = refs[-_OUTBOUND_REF_MAX:]
        return refs

    def _load_outbound_refs(self) -> list[tuple[int, str]]:
        """从 jsonl 加载出站标签记录并裁剪；顺便重写压缩掉过期行。"""
        refs: list[tuple[int, str]] = []
        try:
            with self._refs_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        refs.append((int(obj["ms"]), str(obj["tag"])))
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            logger.warning("读取 ilink 出站标签记录失败，从空开始", exc_info=True)
            return []
        pruned = self._prune_refs(refs)
        if len(pruned) != len(refs):  # 启动时压缩：把过期行落盘清掉，避免文件无限增长
            self._rewrite_outbound_refs(pruned)
        return pruned

    def _rewrite_outbound_refs(self, refs: list[tuple[int, str]]) -> None:
        try:
            self._refs_path.parent.mkdir(parents=True, exist_ok=True)
            with self._refs_path.open("w", encoding="utf-8") as f:
                for ms, tag in refs:
                    f.write(json.dumps({"ms": ms, "tag": tag}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            logger.warning("重写 ilink 出站标签记录失败", exc_info=True)

    def _record_outbound_ref(self, send_ms: int, tag: str) -> None:
        """记录一条「发送时刻 → session 标签」，内存 + 追加落盘。"""
        self._outbound_refs.append((send_ms, tag))
        self._outbound_refs = self._prune_refs(self._outbound_refs)
        try:
            self._refs_path.parent.mkdir(parents=True, exist_ok=True)
            with self._refs_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ms": send_ms, "tag": tag}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            logger.warning("持久化 ilink 出站标签记录失败", exc_info=True)

    def _resolve_ref_by_time(self, create_ms: int) -> str:
        """按被引用消息的 create_time_ms 反查最接近的出站标签（超出容差则空串）。"""
        best_tag = ""
        best_delta = _REF_MATCH_TOLERANCE_MS + 1
        for ms, tag in self._outbound_refs:
            delta = abs(ms - create_ms)
            if delta < best_delta:
                best_delta = delta
                best_tag = tag
        return best_tag if best_delta <= _REF_MATCH_TOLERANCE_MS else ""

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
            # 被引用文本还原：优先用直接解析结果（旧版 ilink 会内联被引用文字）；为空且带
            # ref_create_ms 时（新版只回传时间戳、不含文字），按创建时间反查我们发出的 session 标签。
            quote_text = m.quote_text
            if not quote_text and m.ref_create_ms is not None:
                quote_text = self._resolve_ref_by_time(m.ref_create_ms)
                if not quote_text:
                    logger.warning(
                        "引用消息按时间戳(create_time_ms=%s)未匹配到已记录的会话消息"
                        "（可能引用了修复前发出的历史消息，或非会话消息）",
                        m.ref_create_ms,
                    )
            await self._maybe_send_typing(m.from_user_id)
            msg = IncomingMessage(
                text=text,
                quote_text=quote_text,  # 被引用消息的 [session: <slug>] 标签，供续聊/指令反解
                chatid="",              # 个人微信单聊，无群 chatid
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
                # 记录出站 session 标签 → 发送时刻，供用户引用该消息时按 create_time_ms 反查还原。
                tag = find_session_tag(text)
                if tag:
                    self._record_outbound_ref(int(time.time() * 1000), tag)
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
