"""主进程组装：把 wecom bot、dispatcher、session_manager、db 串起来。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime

from .bot.base import BotRunner
from .bot.factory import create_bot
from .bot.message import IncomingMessage
from .config.schema import AppConfig
from .core.commands import dispatch_command
from .core.dispatcher import DispatchKind, classify
from .core.session_manager import SessionManager
from .core.state import is_open
from .storage.db import Database
from .util.ids import format_session_tag
from .util.logging import setup_logging
from .web.server import WebServer

logger = logging.getLogger(__name__)


def _within_reply_window(
    last_ts: str | None, window_sec: int, now: datetime | None = None
) -> bool:
    """距最后一条机器人回复时刻 ``last_ts`` 是否仍在 ``window_sec`` 秒内。

    用于私聊「窗口内免引用自动续聊」判定。``last_ts`` 为空 / 格式异常 / 与当前时间
    时区不可比（历史脏数据）一律返回 False —— 保守回落"开新会话"，不冒险续到错的会话。
    ``now`` 供测试注入（默认取本地当前时刻）。
    """
    if not last_ts:
        return False
    try:
        last = datetime.fromisoformat(last_ts)
    except (ValueError, TypeError):
        return False
    current = now if now is not None else datetime.now().astimezone()
    try:
        return (current - last).total_seconds() <= window_sec
    except TypeError:
        # last 无时区而 current 带时区（或反之）→ 不可比，保守不续。
        return False


class App:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self._bot: BotRunner | None = None
        self._manager: SessionManager | None = None
        self._web: WebServer | None = None
        self._cleanup_task: asyncio.Task | None = None

    async def _worktree_cleanup_loop(self) -> None:
        """主进程存活期间每小时清理一次过期终态 worktree。"""
        assert self._manager is not None
        while True:
            await asyncio.sleep(3600)
            await self._manager.cleanup_expired_worktrees()

    async def _on_message(self, msg: IncomingMessage) -> None:
        assert self._manager is not None and self._bot is not None
        logger.info(
            "收到消息 chatid=%s userid=%s text=%.80s quote=%.40s",
            msg.chatid, msg.userid, msg.text, msg.quote_text or "",
        )

        async def session_open(s: str) -> bool:
            # 同时认 display_slug 与 internal slug：首次 ack 挂的是 internal slug，
            # 用户引用该消息追加文字时谓词必须命中，才能走 CONTINUE 而不是误判为
            # NEW 再开一个 session。
            row = await self.db.get_session_by_display_slug(s)
            if row is None:
                row = await self.db.get_session(s)
            return row is not None and is_open(row["state"])

        async def session_kind_of(s: str) -> str | None:
            # 仅 /dev（handoff）用：校验被引用的 slug 是不是 chat 会话（返回 session_kind）。
            row = await self.db.get_session_by_display_slug(s)
            if row is None:
                row = await self.db.get_session(s)
            return row.get("session_kind") if row else None

        async def recent_open_chat(userid: str) -> str | None:
            # 私聊窗口内免引用自动续聊：查该用户最近一个活跃 chat，若距最后一条机器人回复
            # ≤ auto_continue_window_sec 则返回其 slug 续到该会话。window<=0 视为关闭。
            # 仅 dispatcher 规则 5 在 chat 模式 + 私聊（chatid 空）下调用，故这里按 userid
            # 查即可（私聊建 row 时 chatid 落的就是 userid）。
            window = self.config.chat.auto_continue_window_sec
            if window <= 0:
                return None
            hit = await self.db.find_recent_open_chat(userid)
            if hit is None:
                return None
            if _within_reply_window(hit.get("last_reply_ts"), window):
                return hit["slug"]
            return None

        decision = await classify(
            msg, self.config, session_open, session_kind_of, recent_open_chat
        )
        chatid = msg.chatid or msg.userid
        if decision.kind == DispatchKind.NEW:
            assert decision.repo is not None
            slug, ahead = await self._manager.new_session(
                repo_cfg=decision.repo,
                text=decision.cleaned_text,
                chatid=chatid,
                userid=msg.userid,
                review_override=decision.review_override,
            )
            # ahead 是 in-flight 总数（在跑 + awaiting + 排队），只有 ahead 触达槽位
            # 上限时新 task 才会真正被 semaphore 挡住排队；否则会立刻 acquire 到槽位开跑。
            # ack 末尾挂 internal slug：display_slug 要 plan 完成后才有，但 internal
            # 在 new_session 同步路径已经分配，挂上后用户可立即引用回复触发 /cancel
            # 或追加文字续推同一 session。
            session_tag = format_session_tag(slug, repo=decision.repo.name)
            # 单需求级 [review] 指令命中时，ack 里明确告知用户本次审查开关已被覆盖
            if decision.review_override is True:
                review_note = "已为本需求开启 Reviewer 审查。"
            elif decision.review_override is False:
                review_note = "已为本需求关闭 Reviewer 审查。"
            else:
                review_note = ""
            if ahead >= self.config.limits.max_concurrent_sessions:
                ack = (
                    f"已加入 @{decision.repo.name} 队列（前面 {ahead} 个）。"
                    f"开始分析时会再通知你。{review_note}\n\n{session_tag}"
                )
            else:
                ack = (
                    f"已收到需求，开始分析 @{decision.repo.name}。"
                    f"当 plan 完成或需要确认时会再通知你。{review_note}\n\n{session_tag}"
                )
            await self._bot.reply(chatid, ack)
            logger.info("dispatch NEW slug=%s ahead=%d", slug, ahead)
        elif decision.kind == DispatchKind.CONTINUE:
            assert decision.session_slug is not None
            ok = await self._manager.continue_session(
                slug=decision.session_slug,
                text=decision.cleaned_text,
                quote_text=msg.quote_text,
            )
            if not ok:
                await self._bot.reply(
                    chatid,
                    f"未找到未结案的 session [{decision.session_slug}]。请重新发起需求。",
                )
        elif decision.kind == DispatchKind.CHAT:
            message = decision.cleaned_text.strip()
            if not message:
                await self._bot.reply(
                    chatid,
                    "用法：`@<repo> /chat <消息>` 开始对话（也可省略 @repo 用回退目录）。",
                )
                return
            display, fallback_note = await self._manager.new_chat_session(
                repo_cfg=decision.repo,
                text=message,
                chatid=chatid,
                userid=msg.userid,
            )
            # ack 末尾挂 tag（含 display_slug + repo），用户可立即引用回复续聊；
            # 首轮输出会补上带 sid 的完整 tag。
            tag = format_session_tag(
                display, repo=decision.repo.name if decision.repo else None
            )
            lead = f"💬 已开始对话 [{display}]，claude 正在思考…"
            if fallback_note:
                lead = f"{fallback_note}\n\n{lead}"
            await self._bot.reply(chatid, f"{lead}\n\n{tag}")
            logger.info(
                "dispatch CHAT slug=%s repo=%s",
                display,
                decision.repo.name if decision.repo else "-",
            )
        elif decision.kind == DispatchKind.HANDOFF:
            assert decision.session_slug is not None
            slug, repo_name, ahead, err = await self._manager.new_pipeline_from_chat(
                chat_slug=decision.session_slug,
                supplement=decision.cleaned_text,
                chatid=chatid,
                userid=msg.userid,
                review_override=decision.review_override,
            )
            if err is not None or slug is None:
                await self._bot.reply(chatid, err or "无法把该对话转为开发任务。")
                return
            # plan 前只有 internal slug（display_slug 要 plan 完成才有）；挂上供用户立即引用回复。
            session_tag = format_session_tag(slug, repo=repo_name)
            if decision.review_override is True:
                review_note = "已为本任务开启 Reviewer 审查。"
            elif decision.review_override is False:
                review_note = "已为本任务关闭 Reviewer 审查。"
            else:
                review_note = ""
            if ahead >= self.config.limits.max_concurrent_sessions:
                ack = (
                    f"已把对话转为开发任务，加入 @{repo_name} 队列（前面 {ahead} 个）。"
                    f"开始规划时会再通知你。{review_note}\n\n{session_tag}"
                )
            else:
                ack = (
                    f"已把对话转为开发任务，开始规划 @{repo_name}。"
                    f"当 plan 完成或需要确认时会再通知你。{review_note}\n\n{session_tag}"
                )
            await self._bot.reply(chatid, ack)
            logger.info(
                "dispatch HANDOFF chat=%s slug=%s ahead=%d",
                decision.session_slug, slug, ahead,
            )
        elif decision.kind == DispatchKind.COMMAND:
            assert decision.command is not None
            reply_texts = await dispatch_command(
                self.db, self._manager, decision.command, decision.command_arg
            )
            # /plan 可能返回多条消息；其它命令也走列表统一路径
            for piece in reply_texts:
                await self._bot.reply(chatid, piece)
        else:
            await self._bot.reply(chatid, decision.reason)

    async def _run_until_signal(self) -> None:
        """跑 bot 长轮询，同时监听 SIGTERM / SIGINT。任一信号到达即返回，让 ``run``
        的 ``finally`` 走优雅退出（取消 session task → engine 回收 agent 子进程组）。

        不装信号处理器时（如收到 SIGTERM），进程被直接杀死、``finally`` 不执行，正在跑
        的 agent 子进程会变成孤儿。某些平台 / 非主线程不支持 add_signal_handler，静默降级。
        """
        assert self._bot is not None
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        installed: list[int] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        bot_task = asyncio.create_task(self._bot.run_forever(), name="bot-run-forever")
        stop_task = asyncio.create_task(stop.wait(), name="await-signal")
        try:
            await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if bot_task.done() and not bot_task.cancelled():
                # bot 自己退出（通常是异常）——原样抛出，交给上层记录
                exc = bot_task.exception()
                if exc is not None:
                    raise exc
            else:
                logger.info("收到停止信号，开始优雅退出")
        finally:
            for sig in installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
            for t in (bot_task, stop_task):
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t

    async def run(self) -> None:
        setup_logging(self.config.log_dir)
        await self.db.connect()
        try:
            self._bot = create_bot(self.config, on_message=self._on_message)
            self._manager = SessionManager(self.db, self.config, self._bot.reply)
            await self._manager.cleanup_expired_worktrees()
            self._cleanup_task = asyncio.create_task(
                self._worktree_cleanup_loop(), name="worktree-cleanup"
            )
            self._web = WebServer(self.db, self.config.http, self.config.workspace_root)
            await self._web.start()
            logger.info("cc-fleet 启动")
            await self._run_until_signal()
        finally:
            if self._cleanup_task is not None:
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    pass
                self._cleanup_task = None
            if self._manager is not None:
                await self._manager.shutdown()
            if self._bot is not None:
                await self._bot.shutdown()
            if self._web is not None:
                await self._web.stop()
            await self.db.close()


def run_app(config: AppConfig) -> None:
    """供 CLI 调用的入口。"""
    asyncio.run(App(config).run())
