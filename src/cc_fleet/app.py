"""主进程组装：把 wecom bot、dispatcher、session_manager、db 串起来。"""

from __future__ import annotations

import asyncio
import logging

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


class App:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self._bot: BotRunner | None = None
        self._manager: SessionManager | None = None
        self._web: WebServer | None = None

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

        decision = await classify(msg, self.config, session_open)
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

    async def run(self) -> None:
        setup_logging(self.config.log_dir)
        await self.db.connect()
        try:
            self._bot = create_bot(self.config, on_message=self._on_message)
            self._manager = SessionManager(self.db, self.config, self._bot.reply)
            self._web = WebServer(self.db, self.config.http, self.config.workspace_root)
            await self._web.start()
            logger.info("cc-fleet 启动")
            await self._bot.run_forever()
        finally:
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
