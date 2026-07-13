"""并发驱动新需求与已有 session。

并发模型：

- ``_slot``：``asyncio.Semaphore(limits.max_concurrent_sessions)``，全局并发槽位。
- ``_repo_locks``：``dict[repo_name, asyncio.Lock]``，per-repo 共享 op 锁；目前给
  ``Session._do_new`` 的 ``fetch_default_branch`` + ``create_worktree`` 用，规避
  ``.git/refs/remotes/origin/<default>`` 的 fs 级竞争。
- ``_sessions``：``dict[internal_slug, _SessionCtx]``，每个 open session 对应一个后台 task。
  task 在 ``async with _slot`` 内长期驻留 —— **awaiting 期间也占着槽位**，避免用户
  回复澄清后还要重新排队。
- ``_pending``：dispatch 同步路径完成"建 db 行"后立即可见的 in-flight 计数（含等
  semaphore + 已 drive + awaiting），用来计算 ack 文案里"前面 N 个"。注意只有
  ``_pending >= max_concurrent_sessions`` 时新 task 才会被 semaphore 挡住排队；否则
  会立刻 acquire 到槽位开跑，ack 应直接回"开始分析"而非"已加入队列"。

dispatch 调用 ``new_session`` / ``continue_session`` 立即返回；真实 drive 在后台 task 完成。

后台 task 异常兜底：``_session_loop`` 顶层 ``except Exception`` 会通过
``_mark_failed_on_drive_exception`` 把 session 转 FAILED 并发通知，避免任何 drive 内
未捕获异常（典型如 ``run_claude`` 抛 ``ValueError``）把 session 悬挂在 working 子态。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config.schema import AppConfig, RepoConfig
from ..storage.db import Database
from ..util.ids import format_session_tag
from .chat import _NO_REPO, ChatSession
from . import repo as repo_module
from .session import ReplyFunc, Session
from .state import SessionState, is_open, is_resumable_terminal, is_terminal

logger = logging.getLogger(__name__)


class _SessionCtx:
    """单 session 的内存上下文：task + 用户回复事件 + 取消标志。"""

    __slots__ = ("session", "task", "resume_event", "cancel_requested")

    def __init__(self, session: Session) -> None:
        self.session = session
        self.task: asyncio.Task | None = None
        self.resume_event = asyncio.Event()
        self.cancel_requested = False


class _ChatCtx:
    """单 chat 会话的内存上下文：task + 用户回复事件 + 取消标志（结构同 _SessionCtx）。"""

    __slots__ = ("chat", "task", "resume_event", "cancel_requested", "turn_lock")

    def __init__(self, chat: ChatSession, turn_lock: asyncio.Lock | None = None) -> None:
        self.chat = chat
        self.turn_lock = turn_lock
        self.task: asyncio.Task | None = None
        self.resume_event = asyncio.Event()
        self.cancel_requested = False


class SessionManager:
    def __init__(self, db: Database, config: AppConfig, reply: ReplyFunc) -> None:
        self.db = db
        self.config = config
        self.reply = reply
        self._slot = asyncio.Semaphore(config.limits.max_concurrent_sessions)
        # /chat 独立并发池：与交付流水线的 _slot 完全隔离，避免长对话饿死 plan/dev。
        self._chat_slot = asyncio.Semaphore(config.chat.max_concurrent)
        self._repo_locks: dict[str, asyncio.Lock] = {}
        # 同一 repo 的 chat 共享一个可被同步的只读 worktree。整轮串行可确保同步时没有
        # 另一个 agent 正在读取，避免用数据库里的长期 awaiting 状态误判“正在使用”。
        self._chat_turn_locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, _SessionCtx] = {}
        self._chats: dict[str, _ChatCtx] = {}
        # dispatch 路径上看见的 in-flight 数（已建 row 但尚未进 terminal）；用来算"前面 N 个"。
        self._pending = 0

    def _repo_lock(self, repo_name: str) -> asyncio.Lock:
        lock = self._repo_locks.get(repo_name)
        if lock is None:
            lock = asyncio.Lock()
            self._repo_locks[repo_name] = lock
        return lock

    def _chat_turn_lock(self, repo_name: str) -> asyncio.Lock:
        lock = self._chat_turn_locks.get(repo_name)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_turn_locks[repo_name] = lock
        return lock

    def _build(self, repo_cfg: RepoConfig) -> Session:
        return Session(
            db=self.db,
            config=self.config,
            repo_cfg=repo_cfg,
            reply=self.reply,
            fetch_lock=self._repo_lock(repo_cfg.name),
        )

    # ---------- dispatch 入口（同步建 db、起后台 task） ----------

    async def new_session(
        self,
        *,
        repo_cfg: RepoConfig,
        text: str,
        chatid: str,
        userid: str,
        review_override: bool | None = None,
    ) -> tuple[str, int]:
        """同步建 db 行 + 起后台 task。返回 (internal_slug, 自己前面有多少个 in-flight session)。

        ``review_override``：单需求级 Reviewer 覆盖（None 跟随 repo 配置 / True 强制开 /
        False 强制关），来自需求文本里的 [review] 内联指令，原样落库。
        """
        session = self._build(repo_cfg)
        await session.create_row(
            initial_request=text,
            chatid=chatid,
            userid=userid,
            review_override=review_override,
        )
        ahead = self._pending  # 自己尚未计入
        self._pending += 1
        ctx = _SessionCtx(session)
        self._sessions[session.slug] = ctx
        ctx.task = asyncio.create_task(
            self._session_loop(ctx),
            name=f"session:{session.slug}",
        )
        return session.slug, ahead

    async def continue_session(
        self,
        *,
        slug: str,
        text: str,
        quote_text: str | None,
    ) -> bool:
        """对一个 open session 喂入用户回复。

        ``slug`` 同时接受 display_slug 与 internal slug（首次 ack 期间用户引用到的是
        internal slug，plan 完成后切到 display_slug——两个都得认）。

        三类路径：
        - AWAITING_USER_CLARIFICATION：复用旧澄清流，apply_clarification + 唤醒
          内存中已等在 ``resume_event`` 上的后台 task。成功时通过
          ``_notify_continue_ack`` 立即回包一句 "已收到补充信息" 让用户感知到
          claude 已被拉起，避免引用回复后无任何反馈。
        - RESUMABLE_TERMINAL（FAILED/TIMEOUT/COMPLETED）：复活流。旧后台 task 已退、
          ``_sessions`` 里没有 ctx；新建 ctx 起新 task 接着 drive。起 task 前同样
          回包 ack，文案按 ``ahead`` 区分是否排队。
        - 其他 working 状态（NEW/PLANNING/DEVELOPING/MR_SUBMITTING）：已经在跑，
          重复 follow-up 拒绝。

        返回 True 表示消息已被处理（成功推进或已回包给用户拒绝提示）；False 表示
        上层应按 "未找到未结案 session" 兜底回复。
        """
        row = await self.db.get_session_by_display_slug(slug)
        if row is None:
            row = await self.db.get_session(slug)
        if row is None or not is_open(row["state"]):
            return False

        # chat 会话走独立分流：状态语义（chatting/chat_awaiting）与 pipeline 不同。
        if row.get("session_kind") == "chat":
            return await self._continue_chat(row, text, quote_text)

        internal = row["slug"]
        display = row.get("display_slug") or internal
        state = SessionState(row["state"])
        ctx = self._sessions.get(internal)

        if state == SessionState.AWAITING_USER_CLARIFICATION:
            if ctx is None or ctx.cancel_requested:
                return False
            ok = await ctx.session.apply_clarification(text, quote_text=quote_text)
            if not ok:
                return False
            # apply_clarification 返回后 row.state 已是 resume 目标（dev 澄清→DEVELOPING、plan 澄清→PLANNING）
            phase_word = (
                "开发"
                if SessionState(ctx.session.row["state"]) == SessionState.DEVELOPING
                else "plan"
            )
            await self._notify_continue_ack(
                row=ctx.session.row,
                text=f"已收到补充信息，claude 继续推进{phase_word} [{display}]。",
            )
            ctx.resume_event.set()
            return True

        if is_resumable_terminal(state):
            if ctx is not None:
                # 防御：状态已 terminal 但 ctx 还在内存（_session_loop finally 还没跑完）。
                # 不起重叠 task。
                logger.warning(
                    "session %s state=%s 但内存 ctx 仍存在，跳过复活",
                    internal, state.value,
                )
                return False
            return await self._wake_resumable(row, text, quote_text)

        # working 状态（非 awaiting）：已经在跑，重复 follow-up 拒绝
        logger.info("session %s state=%s 正在处理，忽略重复 follow-up", internal, state.value)
        return False

    async def _wake_resumable(
        self,
        row: dict[str, Any],
        text: str,
        quote_text: str | None,
    ) -> bool:
        """复活 FAILED/TIMEOUT/COMPLETED session：apply_followup + 起新后台 task。"""
        internal = row["slug"]
        repo_cfg = self.config.repo_by_name_or_alias(row["repo"])
        if repo_cfg is None:
            return False
        session = self._build(repo_cfg)
        await session.resume(internal)
        ok = await session.apply_followup(text, quote_text=quote_text)
        if not ok:
            notice = session._last_followup_notice
            chatid = session.row.get("chatid") or ""
            if notice and chatid:
                try:
                    await self.reply(chatid, notice)
                except Exception:  # noqa: BLE001
                    logger.exception("session %s follow-up 拒绝通知发送失败", internal)
                return True  # 已 ack 拒绝，不让 app 再回兜底
            return False

        display = row.get("display_slug") or internal
        ahead = self._pending  # 自己尚未计入
        # 只有 in-flight 触达 max_concurrent_sessions 时新 task 才会被 semaphore 挡住；
        # 否则会立刻拿到槽位开跑，不应回排队文案。
        if ahead >= self.config.limits.max_concurrent_sessions:
            ack = (
                f"已收到回复 [{display}]，前面 {ahead} 个，"
                "开始处理时再通知你。"
            )
        else:
            ack = f"已收到回复，claude 正在继续推进 [{display}]。"
        await self._notify_continue_ack(row=session.row, text=ack)

        ctx = _SessionCtx(session)
        self._sessions[internal] = ctx
        self._pending += 1
        ctx.task = asyncio.create_task(
            self._session_loop(ctx),
            name=f"session:{internal}",
        )
        return True

    async def _notify_continue_ack(self, *, row: dict[str, Any], text: str) -> None:
        """CONTINUE 路径上回包给用户的即时 ack：消息后追加 session tag，便于用户后续引用。

        chatid 缺失时降级到 userid（与 ``App._on_message`` 的回包路由一致）。reply 失败
        只记日志，不影响后台 drive。
        """
        chatid = row.get("chatid") or row.get("userid") or ""
        if not chatid:
            return
        tag = format_session_tag(
            row.get("display_slug") or row.get("slug") or "",
            repo=row.get("repo"),
            claude_session_id=row.get("claude_session_id"),
        )
        try:
            await self.reply(chatid, f"{text}\n\n{tag}")
        except Exception:  # noqa: BLE001 - ack 失败不应阻塞 drive
            logger.exception("session %s continue ack 发送失败", row.get("slug") or "")

    # ---------- /chat 通道 ----------

    async def new_chat_session(
        self,
        *,
        repo_cfg: RepoConfig | None,
        text: str,
        chatid: str,
        userid: str,
    ) -> tuple[str, str | None]:
        """同步建 chat row + 起后台 _chat_loop。返回 (display_slug, 回退警告或 None)。

        无 @repo 时回退到 chat.default_cwd → 用户 home，并生成一条警告文案由上层拼进 ack。
        chat 不占用 ``_slot``，也不计入 ``_pending``（走独立 ``_chat_slot`` 池）。
        """
        fallback_cwd: Path | None = None
        note: str | None = None
        if repo_cfg is None:
            cfg_cwd = self.config.chat.default_cwd
            fallback_cwd = (cfg_cwd or Path.home()).expanduser()
            src = "chat.default_cwd 配置" if cfg_cwd else "用户 home 目录"
            note = (
                f"⚠️ 未指定 @repo，本次 chat 在回退目录 `{fallback_cwd}`（{src}）中只读运行，"
                "读不到具体仓库代码。建议用 `@<repo> /chat …` 绑定仓库，讨论才能基于该仓库代码。"
            )
        chat = ChatSession(
            db=self.db,
            config=self.config,
            reply=self.reply,
            repo_cfg=repo_cfg,
            fallback_cwd=fallback_cwd,
            fetch_lock=(
                self._repo_lock(repo_cfg.name) if repo_cfg is not None else None
            ),
        )
        display = await chat.create_row(text=text, chatid=chatid, userid=userid)
        ctx = _ChatCtx(
            chat,
            self._chat_turn_lock(repo_cfg.name) if repo_cfg is not None else None,
        )
        self._chats[chat.slug] = ctx
        ctx.task = asyncio.create_task(self._chat_loop(ctx), name=f"chat:{chat.slug}")
        return display, note

    async def _continue_chat(
        self, row: dict[str, Any], text: str, quote_text: str | None
    ) -> bool:
        """把用户后续输入喂给一个 open 的 chat 会话。

        - 内存有活跃 ctx：CHATTING（上一轮在跑）回"稍候"；CHAT_AWAITING/可恢复终态 →
          apply_user_message + 唤醒 resume_event。
        - 无 ctx（进程重启 / loop 已退出，含 CHATTING 孤儿）→ 重建 task 复活。
        """
        internal = row["slug"]
        display = row.get("display_slug") or internal
        ctx = self._chats.get(internal)
        if ctx is not None:
            ok = await ctx.chat.apply_user_message(text, quote_text=quote_text)
            if not ok:
                await self._reply_safe(
                    row, f"chat [{display}] 正在处理上一条消息，请等它回复后再发。"
                )
                return True
            ctx.resume_event.set()
            return True
        return await self._revive_chat(row, text, quote_text)

    async def _revive_chat(
        self, row: dict[str, Any], text: str, quote_text: str | None
    ) -> bool:
        """无内存 ctx 时重建 ChatSession + 起新 _chat_loop（抗进程重启）。"""
        internal = row["slug"]
        display = row.get("display_slug") or internal
        repo_cfg = self.config.repo_by_name_or_alias(row["repo"])
        fallback_cwd = None
        if repo_cfg is None:
            fallback_cwd = (self.config.chat.default_cwd or Path.home()).expanduser()
        chat = ChatSession(
            db=self.db,
            config=self.config,
            reply=self.reply,
            repo_cfg=repo_cfg,
            fallback_cwd=fallback_cwd,
            fetch_lock=(
                self._repo_lock(repo_cfg.name) if repo_cfg is not None else None
            ),
        )
        await chat.resume(internal)
        # 无条件注入用户消息 + 转 CHATTING（孤儿可能停在任意 open 态）。
        await self.db.add_message(internal, "in", text, quote_text=quote_text)
        chat.pending_user_message = text
        await chat._set_state(SessionState.CHATTING)
        ctx = _ChatCtx(
            chat,
            self._chat_turn_lock(repo_cfg.name) if repo_cfg is not None else None,
        )
        self._chats[internal] = ctx
        ctx.task = asyncio.create_task(
            self._chat_loop(ctx), name=f"chat:{internal}:revive"
        )
        await self._reply_safe(row, f"已收到，继续 chat [{display}]。")
        return True

    async def _cancel_chat(self, row: dict[str, Any]) -> bool:
        internal = row["slug"]
        ctx = self._chats.get(internal)
        if ctx is not None:
            ctx.cancel_requested = True
            await ctx.chat.cancel()
            ctx.resume_event.set()
            return True
        chat = ChatSession(
            db=self.db, config=self.config, reply=self.reply, repo_cfg=None
        )
        await chat.resume(internal)
        await chat.cancel()
        return True

    async def _chat_loop(self, ctx: _ChatCtx) -> None:
        """一个 chat 会话的后台驱动：反复 run_turn，CHAT_AWAITING 时挂起等用户回复。

        与 _session_loop 的关键区别：用独立 ``_chat_slot``，且**只在跑一轮时**占槽——
        CHAT_AWAITING 挂起期间释放，避免闲置 chat 长期占并发。
        """
        slug = ctx.chat.slug
        try:
            while not ctx.cancel_requested:
                async with self._chat_slot:
                    if ctx.cancel_requested:
                        break
                    if ctx.turn_lock is None:
                        await ctx.chat.run_turn()
                    else:
                        # setup 可能同步共享 worktree，必须与使用该树的整轮 agent 互斥。
                        async with ctx.turn_lock:
                            await ctx.chat.run_turn()
                state = SessionState(ctx.chat.row["state"])
                if state != SessionState.CHAT_AWAITING:
                    break  # FAILED / CANCELLED → 退出
                await ctx.resume_event.wait()
                ctx.resume_event.clear()
        except asyncio.CancelledError:
            logger.info("chat %s 后台 task 被取消", slug)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat %s loop 异常", slug)
            await self._mark_chat_failed(ctx, exc)
        finally:
            self._chats.pop(slug, None)

    async def _mark_chat_failed(self, ctx: _ChatCtx, exc: BaseException) -> None:
        """chat loop 抛异常后兜底转 FAILED（已终态则跳过，避免覆盖已发的失败）。"""
        chat = ctx.chat
        try:
            state = SessionState(chat.row.get("state") or "")
        except ValueError:
            state = None
        if state is not None and is_terminal(state):
            return
        first_line = (str(exc).strip().splitlines() or [""])[0]
        summary = (
            f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__
        )
        try:
            await chat._set_state(
                SessionState.FAILED, last_error=f"chat 主控异常：{summary}"
            )
            await chat._notify(f"❌ chat 会话异常中断：{summary}{chat._tag()}")
        except Exception:  # noqa: BLE001
            logger.exception("chat %s 兜底 fail 失败", chat.slug)

    async def _reply_safe(self, row: dict[str, Any], text: str) -> None:
        """给 chat 用户回一句短消息（chatid 缺失降级 userid）；失败只记日志。"""
        chatid = row.get("chatid") or row.get("userid") or ""
        if not chatid:
            return
        try:
            await self.reply(chatid, text)
        except Exception:  # noqa: BLE001
            logger.exception("chat %s 回复失败", row.get("slug") or "")

    # ---------- /chat → pipeline handoff（/dev） ----------

    async def new_pipeline_from_chat(
        self,
        *,
        chat_slug: str,
        supplement: str,
        chatid: str,
        userid: str,
        review_override: bool | None = None,
    ) -> tuple[str | None, str | None, int, str | None]:
        """把一条 /chat 对话转成正式开发 pipeline（/dev handoff）。

        复用被转入 chat 的 ``claude_session_id``（新 pipeline 首轮 --resume 整段讨论），新建
        一个 ``session_kind='pipeline'`` 的 row 从 NEW 起走完整流水线；随后归档原 chat。

        返回 ``(internal_slug, repo_name, ahead, error)``：error 非 None 时前三项为占位
        （None/None/0），由上层原样回给用户。所有前置校验失败都在这里以中文 error 返回，
        不建 row、不起 task。
        """
        row = await self.db.get_session_by_display_slug(chat_slug)
        if row is None:
            row = await self.db.get_session(chat_slug)
        if row is None:
            return None, None, 0, (
                f"未找到该 /chat 会话 [{chat_slug}]。请引用一条 /chat 对话的机器人消息再发 /dev。"
            )

        display = row.get("display_slug") or row["slug"]
        if row.get("session_kind") != "chat":
            return None, None, 0, (
                f"/dev 只能把 /chat 对话转成开发任务；[{display}] 不是 chat 会话。"
            )

        state = SessionState(row["state"])
        if state == SessionState.CHATTING:
            return None, None, 0, (
                f"chat [{display}] 正在生成回复，请等它回复完再 /dev。"
            )

        repo_name = row["repo"]
        if repo_name == _NO_REPO:
            return None, None, 0, (
                "这条 chat 没有绑定仓库（当初用的是裸 /chat），无法直接转开发。"
                "请用 `@<repo> /chat` 重新聊清楚后再 /dev，或直接 `@<repo> 需求` 开发。"
            )

        repo_cfg = self.config.repo_by_name_or_alias(repo_name)
        if repo_cfg is None:
            return None, None, 0, (
                f"chat 所属仓库 `{repo_name}` 不在当前配置中，无法转开发。"
            )

        csid = row.get("claude_session_id")
        if not csid:
            return None, None, 0, (
                "这条 chat 还没成功回复过（claude 会话尚未建立），无法 /dev。"
                "请等它回复后再试。"
            )

        chat_internal = row["slug"]
        if await self.db.session_exists_with_origin(chat_internal):
            return None, None, 0, (
                "这条 chat 已经转过开发任务了，请引用该开发任务的机器人消息继续。"
            )

        session = self._build(repo_cfg)
        initial_request = _compose_handoff_request(row.get("initial_request"), supplement)
        try:
            await session.create_row(
                initial_request=initial_request,
                chatid=chatid,
                userid=userid,
                review_override=review_override,
                claude_session_id=csid,
                origin_chat_slug=chat_internal,
            )
        except sqlite3.IntegrityError:
            # 并发双 /dev：部分唯一索引 idx_sessions_origin_chat 原子挡住第二个。
            return None, None, 0, (
                "这条 chat 已经转过开发任务了，请引用该开发任务的机器人消息继续。"
            )

        # 归档原 chat：置 CANCELLED + 提示。放在起 task 之前，确保原 chat loop 不会再起一轮
        # 而与新 pipeline 抢同一个 claude 会话。
        await self._archive_chat_after_handoff(chat_internal, session.slug)

        ahead = self._pending  # 自己尚未计入
        self._pending += 1
        ctx = _SessionCtx(session)
        self._sessions[session.slug] = ctx
        ctx.task = asyncio.create_task(
            self._session_loop(ctx),
            name=f"session:{session.slug}:handoff",
        )
        logger.info(
            "handoff：chat %s → pipeline %s（复用 csid，从 PLANNING 重规划）",
            chat_internal, session.slug,
        )
        return session.slug, repo_cfg.name, ahead, None

    async def _archive_chat_after_handoff(
        self, chat_internal: str, pipeline_slug: str
    ) -> None:
        """把被转入的 chat 归档为 CANCELLED（复用内存 ctx 或建临时对象），并唤醒其 loop 退出。"""
        ctx = self._chats.get(chat_internal)
        if ctx is not None:
            ctx.cancel_requested = True
            await ctx.chat.mark_handed_off(pipeline_slug)
            ctx.resume_event.set()
            return
        chat = ChatSession(
            db=self.db, config=self.config, reply=self.reply, repo_cfg=None
        )
        await chat.resume(chat_internal)
        await chat.mark_handed_off(pipeline_slug)

    # ---------- 显式恢复 ----------

    async def resume_session(self, slug: str) -> tuple[bool, str]:
        """聊天端 /resume：把一个 working 中的 session 重新挂上后台 task 继续推进。

        典型用法：主控曾被 kill,db 留下 state=developing/planning/... 的孤儿 row,
        但内存无 ctx、引用回复也走不动（不在 awaiting、也不在 resumable_terminal)。
        用户用 `/resume <slug>` 显式拉起。

        slug 既支持 display_slug 也支持 internal slug,与 ``/cancel`` 一致。

        返回 ``(True, ack)`` 表示已起后台 task,ack 给上层回包用户。
        返回 ``(False, reason)`` 时 reason 是给用户的中文拒绝原因。

        拒绝场景：
        - 找不到 slug
        - 已经在内存中（task 还活着,无需再起）
        - awaiting → 引导用户用引用回复回答澄清问题
        - completed/failed/timeout → 引导用户用引用回复唤醒
        - cancelled → 不可恢复,引导重新发起需求
        - 仓库已从 config 移除 / local worktree 丢失 → 无法恢复
        """
        row = await self.db.get_session_by_display_slug(slug)
        if row is None:
            row = await self.db.get_session(slug)
        if row is None:
            return False, f"未找到 session [{slug}]。"

        internal = row["slug"]
        display = row.get("display_slug") or internal
        state = SessionState(row["state"])

        if internal in self._sessions:
            return False, (
                f"session [{display}] 已经在主控内存中（state={state.value}）,无需 /resume。"
            )

        if state == SessionState.AWAITING_USER_CLARIFICATION:
            return False, (
                f"session [{display}] 正在等你的澄清回复。"
                "请**引用**机器人之前发的 plan 反问消息来回答,而不是用 /resume。"
            )

        if is_resumable_terminal(state):
            return False, (
                f"session [{display}] 已 {state.value},不需要 /resume。"
                "请**引用**该 session 的最近一条机器人消息再追加内容,即可唤醒继续推进。"
            )

        if state == SessionState.CANCELLED:
            return False, (
                f"session [{display}] 已被取消（cancelled),不可恢复。"
                "如需重做请 @<repo> 重新发起需求。"
            )

        # 现在只剩 NEW / PLANNING / DEVELOPING / MR_SUBMITTING 四种 working 状态
        repo_cfg = self.config.repo_by_name_or_alias(row["repo"])
        if repo_cfg is None:
            return False, (
                f"session [{display}] 所属仓库 `{row.get('repo')}` 不在当前 config 中,"
                "无法恢复。"
            )

        # local 模式 worktree 在主机本地,resume 前校验存在。NEW 状态 _do_new
        # 还会自己创建,跳过。remote 模式 worktree 在远端,不在主控侧预判,让 claude 自己报错走 _fail。
        if (
            repo_cfg.mode == "local"
            and state != SessionState.NEW
            and not _worktree_exists(row.get("worktree_path"))
        ):
            return False, (
                f"session [{display}] 的 worktree 已丢失,无法恢复。"
                "请 @<repo> 重新发起需求。"
            )

        session = self._build(repo_cfg)
        await session.resume(internal)

        ctx = _SessionCtx(session)
        self._sessions[internal] = ctx
        self._pending += 1
        ctx.task = asyncio.create_task(
            self._session_loop(ctx),
            name=f"session:{internal}:resume",
        )
        logger.info("session %s 通过 /resume 显式恢复（state=%s）", internal, state.value)
        return True, (
            f"session [{display}] 已恢复推进（state={state.value}）。"
            "后续 plan 完成 / 需要确认 / 完成 MR 时会再通知你。"
        )

    async def cancel(self, slug: str) -> bool:
        """聊天端 /cancel 或 CLI cancel：把 session 置为 CANCELLED 并唤醒后台 task。

        软取消：不强 kill 正在跑的 claude 子进程，让 drive loop 在下一轮发现 state 已
        终态后自然退出。awaiting 中的 session 通过 resume_event 立刻唤醒。
        参数 slug 既支持 internal 也支持 display：先用 display 查，再退化到 internal。
        """
        row = await self.db.get_session_by_display_slug(slug)
        if row is None:
            row = await self.db.get_session(slug)
        if row is None or not is_open(row["state"]):
            return False
        if row.get("session_kind") == "chat":
            return await self._cancel_chat(row)
        internal = row["slug"]
        ctx = self._sessions.get(internal)
        if ctx is not None:
            ctx.cancel_requested = True
            await ctx.session.cancel()
            ctx.resume_event.set()
            return True

        # 没有内存上下文：可能是历史进程留下的 open 行；直接落 db。
        repo_cfg = self.config.repo_by_name_or_alias(row["repo"])
        if repo_cfg is None:
            return False
        session = self._build(repo_cfg)
        await session.resume(internal)
        await session.cancel()
        return True

    async def hard_cancel(self, slug: str) -> bool:
        """聊天端 /kill：**强杀**——先按下 kill_event 让 engine 监控循环立即杀掉正在跑的
        活进程组（run 返回 ``killed=True``），再复用 ``cancel()`` 落 CANCELLED + 回执。

        与 ``cancel()``（软取消，不打断活进程、等 claude 自己收尾）并存互补。无内存 ctx
        （孤儿行 / 已无活进程可杀）时退化为等价软取消：没有活进程，强杀无从谈起。
        参数 slug 同 ``cancel``：先 display 再 internal。"""
        row = await self.db.get_session_by_display_slug(slug)
        if row is None:
            row = await self.db.get_session(slug)
        if row is None or not is_open(row["state"]):
            return False
        internal = row["slug"]
        # 先按下强杀信号：engine 监控循环下一轮（≤1s）响应，杀活进程组。
        if row.get("session_kind") == "chat":
            chat_ctx = self._chats.get(internal)
            if chat_ctx is not None:
                chat_ctx.chat.hard_kill()
        else:
            ctx = self._sessions.get(internal)
            if ctx is not None:
                ctx.session.hard_kill()
        # 落 CANCELLED + 唤醒后台 task + 回执：复用软取消路径（幂等；已 set 的
        # kill_event 不受影响，被杀的 run 返回后调用点会因 CANCELLED 守卫静默收尾）。
        return await self.cancel(slug)

    async def shutdown(self) -> None:
        """主进程退出：取消所有 task，等 drain（含 chat）。"""
        ctxs = list(self._sessions.values()) + list(self._chats.values())
        for c in ctxs:
            c.cancel_requested = True
            c.resume_event.set()
            if c.task is not None and not c.task.done():
                c.task.cancel()
        tasks = [c.task for c in ctxs if c.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def list_sessions(self, *, state: SessionState | None = None) -> list[dict[str, Any]]:
        return await self.db.list_sessions(state.value if state else None)

    async def cleanup_expired_worktrees(self) -> int:
        """清理超过保留期的 completed/cancelled 本地 worktree，返回成功数量。

        failed/timeout 仍可恢复，remote 路径也不由本地主控删除。认领动作在数据库中原子
        清空 ``worktree_path``，与 completed follow-up 竞争时只会有一方成功。
        """
        cutoff = datetime.now().astimezone() - timedelta(
            hours=self.config.worktree_retention_hours
        )
        cleaned = 0
        for row in await self.db.list_sessions():
            if row.get("state") not in {
                SessionState.COMPLETED.value,
                SessionState.CANCELLED.value,
            }:
                continue
            raw_path = row.get("worktree_path")
            if not raw_path:
                continue
            repo_cfg = self.config.repo_by_name_or_alias(row.get("repo") or "")
            if repo_cfg is None or repo_cfg.mode != "local":
                continue
            try:
                updated = datetime.fromisoformat(row["updated_at"])
                if updated.tzinfo is None:
                    updated = updated.astimezone()
            except (KeyError, TypeError, ValueError):
                logger.warning("session %s updated_at 非法，跳过 worktree 清理", row.get("slug"))
                continue
            if updated > cutoff:
                continue

            worktree = Path(raw_path).expanduser().resolve()
            expected_root = repo_cfg.path.with_name(
                repo_cfg.path.name + "-worktrees"
            ).resolve()
            if not worktree.is_relative_to(expected_root) or worktree.name == "_chat":
                logger.error(
                    "session %s worktree=%s 越出预期根目录 %s，拒绝自动清理",
                    row["slug"], worktree, expected_root,
                )
                continue
            claimed = await self.db.claim_worktree_cleanup(
                row["slug"], raw_path, row["updated_at"]
            )
            if not claimed:
                continue
            try:
                if worktree.exists():
                    async with self._repo_lock(repo_cfg.name):
                        await repo_module.remove_worktree(
                            repo_cfg.path, worktree, force=True
                        )
            except Exception:  # noqa: BLE001
                await self.db.restore_worktree_after_cleanup(row["slug"], raw_path)
                logger.exception("session %s 自动清理 worktree 失败", row["slug"])
                continue
            cleaned += 1
            logger.info("session %s 过期 worktree 已清理：%s", row["slug"], worktree)
        return cleaned

    # ---------- 后台 driver ----------

    async def _session_loop(self, ctx: _SessionCtx) -> None:
        """一个 session 从 NEW 到 terminal 的全生命周期。

        - acquire semaphore；拿到瞬间如果排过队就发 "开始分析" 通知。
        - 反复 drive：跑到 awaiting 时 wait resume_event；用户回复触发 apply_clarification
          后再 drive。state 走到 terminal 或 cancel_requested 后退出。
        - awaiting **占着** semaphore（避免用户回复后还要重新排队的糟糕体验）。
        """
        slug = ctx.session.slug
        try:
            # 拿不到槽位即视为排队；asyncio.Semaphore 没暴露公共计数 API，借 locked() 近似判断。
            queued = self._slot.locked()
            async with self._slot:
                if queued and not ctx.cancel_requested:
                    repo_name = ctx.session.row.get("repo", "")
                    chatid = ctx.session.row.get("chatid") or ""
                    if chatid:
                        try:
                            await self.reply(chatid, f"@{repo_name} 开始分析 [{slug}]。")
                        except Exception:  # noqa: BLE001 - reply 失败不应阻塞 drive
                            logger.exception("session %s 排队后通知失败", slug)

                while True:
                    if ctx.cancel_requested:
                        break
                    await ctx.session.drive()  # 跑到 awaiting 或终态
                    state = SessionState(ctx.session.row["state"])
                    if state != SessionState.AWAITING_USER_CLARIFICATION:
                        break
                    # awaiting：等用户回复（apply_clarification + set event）或 cancel
                    await ctx.resume_event.wait()
                    ctx.resume_event.clear()
        except asyncio.CancelledError:
            logger.info("session %s 后台 task 被取消", slug)
            raise
        except Exception as exc:  # noqa: BLE001
            # drive 抛任何未捕获异常时必须把 session 转 FAILED：否则 DB state 留在
            # working 子态（典型 planning / developing），调度槽虽然由 finally 释放，
            # 但用户从 /list 与前端看到的就是"一直 working 不动"，也收不到失败通知。
            logger.exception("session %s drive 异常", slug)
            await self._mark_failed_on_drive_exception(ctx, exc)
        finally:
            self._pending = max(0, self._pending - 1)
            self._sessions.pop(slug, None)

    async def _mark_failed_on_drive_exception(
        self, ctx: _SessionCtx, exc: BaseException
    ) -> None:
        """drive 抛异常后兜底：把 session 转 FAILED 并通知用户。

        last_error 只存异常类型 + 首行 message 的概要（完整 traceback 已经由
        ``logger.exception`` 写到 app.log，不在 DB 里重复存）。如果 session 已经在终态
        （cancel / 主动 _fail 后再抛），不做覆盖。``_fail`` 自身失败时只打日志，避免
        递归抛异常把 finally 也带挂。
        """
        slug = ctx.session.slug
        try:
            state = SessionState(ctx.session.row.get("state") or "")
        except ValueError:
            state = None
        if state is not None and is_terminal(state):
            return
        first_line = (str(exc).strip().splitlines() or [""])[0]
        summary = f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__
        reason = f"主控异常未捕获：{summary}"
        try:
            await ctx.session._fail(reason)
        except Exception:  # noqa: BLE001
            logger.exception("session %s 兜底 _fail 失败", slug)


def _worktree_exists(path: str | None) -> bool:
    """local 模式 worktree 完整性检查。None / 空字符串 / 不是目录均返 False。"""
    if not path:
        return False
    return Path(path).is_dir()


def _compose_handoff_request(chat_first: str | None, supplement: str | None) -> str:
    """组装 handoff pipeline 的 initial_request：对话最初需求 + 转开发补充说明。

    真正的讨论上下文靠 --resume 复用 chat 会话承载，这里的文本只作兜底 / 审计（也会喂给
    独立 Reviewer——它不 resume chat）。任一为空则省略对应段落。
    """
    parts = ["【由 /chat 对话转入的开发任务】"]
    first = (chat_first or "").strip()
    if first:
        parts.append(f"对话最初的需求描述：\n{first}")
    sup = (supplement or "").strip()
    if sup:
        parts.append(f"转开发时的补充说明：\n{sup}")
    return "\n\n".join(parts)
