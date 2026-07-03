"""`/chat` 自由对话通道的会话对象。

与 ``core/session.py`` 的交付流水线（plan→dev→MR 强协议状态机）**互斥**：chat 是一条
轻量路径，把 Claude 当成通过微信透出来的多轮对话窗口。cc-fleet 只做 I/O 管道——每收到
一条用户输入就用 ``--resume`` 起一次性 claude 子进程跑完这一轮，把完整输出分段回发。

复用现有基础设施，不重复造轮子：

- 建 worktree：``repo.fetch_default_branch`` + ``create_worktree``（与 Session._do_new 同约定）。
- 护栏：``runner.guardrail.prepare`` 写 settings.json + ``CC_FLEET_WORKTREE`` 边界。
- 分段：``util.text.split_for_chat``。
- session tag：``util.ids.format_session_tag``（用户引用回复即续聊，见 dispatcher 规则 1）。
- 存储：复用 ``sessions`` 表（``session_kind='chat'`` 区分），messages / events 表照常。

chat 行走两个状态（见 core/state.py）：``CHATTING``（一轮在跑）↔ ``CHAT_AWAITING``（等
用户引用回复）。终态复用 ``CANCELLED``（/cancel）与 ``FAILED``（子进程报错，可引用回复唤醒）。
并发调度、续聊唤醒、抗重启都在 ``SessionManager`` 里（``_chat_loop`` / ``_continue_chat``）。
"""

from __future__ import annotations

import logging
import secrets
from importlib import resources
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config.schema import AgentTool, AppConfig, RepoConfig
from ..storage.db import Database
from ..util.ids import format_session_tag, new_internal_slug, new_uuid
from ..util.text import DEFAULT_CHAT_CHUNK_LIMIT, split_for_chat
from . import repo as repo_module
from .runner_factory import get_runner
from .runners.base import AgentPermission
from .runners.claude import format_run_failure
from .slug import resolve_slug_conflict
from .state import SessionState

logger = logging.getLogger(__name__)

ReplyFunc = Callable[[str, str], Awaitable[None]]

# repo 列 NOT NULL，无 @repo 绑定的 chat 用此哨兵占位；渲染 tag / 表格时视作"无 repo"。
_NO_REPO = "-"

# 一轮 claude 没有任何文本输出（例如只做了工具调用）时的兜底文案，避免用户干等。
_EMPTY_OUTPUT_NOTICE = "（本轮 claude 没有产生文本输出）"


def _chat_protocol_text() -> str:
    """chat 阶段注入的极简 system prompt（不含任何输出协议）。"""
    return (
        resources.files("cc_fleet.prompts")
        .joinpath("chat_protocol.md")
        .read_text(encoding="utf-8")
    )


class ChatSession:
    """单个 /chat 多轮对话的驱动器。

    生命周期由 ``SessionManager`` 的后台 ``_chat_loop`` 驱动：``ensure_setup_once`` →
    循环 ``run_turn``；每轮之间在 ``CHAT_AWAITING`` 挂起等用户引用回复。
    """

    def __init__(
        self,
        *,
        db: Database,
        config: AppConfig,
        reply: ReplyFunc,
        repo_cfg: RepoConfig | None,
        fallback_cwd: Path | None = None,
        fetch_lock: Any = None,
    ) -> None:
        self.db = db
        self.config = config
        self.reply = reply
        self._repo_cfg = repo_cfg
        # 无 @repo 时的回退工作目录（SessionManager 解析好传入）；有 repo 时忽略。
        self._fallback_cwd = fallback_cwd
        self._fetch_lock = fetch_lock
        self.runner = get_runner(
            repo_cfg.agent if repo_cfg is not None else AgentTool.CLAUDE, config
        )

        self.slug: str = ""
        self.row: dict[str, Any] = {}
        # 由 apply_user_message 注入的下一轮用户输入；首轮为 None（用 initial_request）。
        self.pending_user_message: str | None = None

    # ---------- 建 row / 加载 ----------

    async def create_row(self, *, text: str, chatid: str, userid: str) -> str:
        """同步建 db 行（不 drive）。返回 display_slug 供 ack 使用。

        display_slug 走 ``chat-<hex>`` + 冲突消解，与 pipeline 的 ``req-…`` internal slug
        命名空间不撞。初始 state=CHATTING：首轮由后台 loop 立即跑。
        """
        self.slug = new_internal_slug()
        display = await resolve_slug_conflict(
            f"chat-{secrets.token_hex(2)}", self.db.display_slug_exists
        )
        repo_name = self._repo_cfg.name if self._repo_cfg is not None else _NO_REPO
        default_branch = (
            self._repo_cfg.default_branch if self._repo_cfg is not None else _NO_REPO
        )
        await self.db.insert_session(
            {
                "slug": self.slug,
                "display_slug": display,
                "repo": repo_name,
                "state": SessionState.CHATTING.value,
                "claude_session_id": None,
                "worktree_path": None,
                "branch": None,
                "default_branch": default_branch,
                "initial_request": text,
                "chatid": chatid,
                "userid": userid,
                "session_kind": "chat",
            }
        )
        await self.db.add_message(self.slug, "in", text)
        await self.db.add_event(
            self.slug,
            "chat_started",
            {"repo": repo_name if repo_name != _NO_REPO else None},
        )
        await self._refresh_row()
        logger.info("chat %s 启动：display=%s repo=%s", self.slug, display, repo_name)
        return display

    async def resume(self, slug: str) -> None:
        """从已有 slug 加载 row（进程重启后由 _continue_chat 重建 ctx 时用）。"""
        self.slug = slug
        await self._refresh_row()

    # ---------- 环境准备 ----------

    async def ensure_setup_once(self) -> None:
        """幂等准备工作目录：首次建 worktree（local repo）或解析回退 cwd。

        已有 ``worktree_path``（首轮已建 / 进程重启后 row 仍在）直接返回，天然抗重启。
        local repo：复用 ``fetch_default_branch`` + ``create_worktree``（fetch_lock 下串行，
        规避 .git 竞争），分支 ``chat/<slug>``。无 repo 或 remote repo：不建 worktree，
        直接在回退目录 / repo 壳子目录运行。建 worktree 失败 → 转 FAILED 并抛出，让 loop 退出。
        """
        await self._refresh_row()
        if self.row.get("worktree_path"):
            return

        if self._repo_cfg is not None and self._repo_cfg.mode == "local":
            worktree_root = self._repo_cfg.path.with_name(
                self._repo_cfg.path.name + "-worktrees"
            )
            worktree_path = worktree_root / self.slug
            branch = f"chat/{self.slug}"
            base = f"origin/{self._repo_cfg.default_branch}"
            already = worktree_path.exists() and (worktree_path / ".git").exists()
            if not already:
                try:
                    if self._fetch_lock is not None:
                        async with self._fetch_lock:
                            await repo_module.fetch_default_branch(
                                self._repo_cfg.path, self._repo_cfg.default_branch
                            )
                            await repo_module.create_worktree(
                                self._repo_cfg.path, worktree_path, branch, base
                            )
                    else:
                        await repo_module.fetch_default_branch(
                            self._repo_cfg.path, self._repo_cfg.default_branch
                        )
                        await repo_module.create_worktree(
                            self._repo_cfg.path, worktree_path, branch, base
                        )
                except repo_module.GitError as e:
                    await self._set_state(
                        SessionState.FAILED, last_error=f"创建 chat worktree 失败：{e}"
                    )
                    await self._notify(f"❌ 创建 chat worktree 失败：{e}{self._tag()}")
                    raise
            await self._update(worktree_path=str(worktree_path), branch=branch)
        else:
            # 无 repo（回退到 default_cwd / home）或 remote repo（用其壳子目录）。
            if self._repo_cfg is not None:
                cwd = self._repo_cfg.path
            else:
                cwd = self._fallback_cwd or Path.home()
            cwd = cwd.expanduser()
            cwd.mkdir(parents=True, exist_ok=True)
            await self._update(worktree_path=str(cwd), branch=None)

    # ---------- 一轮对话 ----------

    async def run_turn(self) -> None:
        """跑一轮 claude 并把输出分段回发，然后转 CHAT_AWAITING。

        首轮（claude_session_id 为空）用 ``--session-id``；之后 ``--resume`` 续接上下文。
        prompt 取 pending_user_message（续聊）或 initial_request（首轮）。失败 / 超时 →
        FAILED（可引用回复唤醒重试，续接同一 claude 会话）。
        """
        await self.ensure_setup_once()
        await self._refresh_row()
        cwd = Path(self.row["worktree_path"])
        guardrail = self.runner.guardrail.prepare(
            settings_dir=self._session_dir() / ".cc-fleet"
        )
        stream_log = self._session_dir() / "stream.jsonl"

        prompt = (
            self.pending_user_message
            if self.pending_user_message is not None
            else self.row["initial_request"]
        )
        self.pending_user_message = None
        existing_sid = self.row.get("claude_session_id")
        session_id = existing_sid or new_uuid()
        resume_from = existing_sid  # None 时首轮走 --session-id

        result = await self.runner.run(
            prompt=prompt,
            cwd=cwd,
            permission=AgentPermission.WRITE,
            protocol_text=_chat_protocol_text(),
            session_id=session_id,
            resume_from=resume_from,
            guardrail=guardrail,
            timeout_sec=self.config.chat.turn_timeout_sec,
            stream_log_path=stream_log,
            extra_env={"CC_FLEET_WORKTREE": str(cwd)},
            on_event=self._persist_event,
        )

        if result.timed_out:
            await self._set_state(SessionState.FAILED, last_error="chat 轮次超时")
            await self._notify(f"⏱️ 本轮超时，会话已中断。可引用本消息重试。{self._tag()}")
            return
        if result.exit_code not in (0, None) or result.result_is_error:
            await self._set_state(SessionState.FAILED, last_error="chat claude 运行失败")
            await self._notify(f"❌ {format_run_failure(result, 'chat')}{self._tag()}")
            return

        # 成功才落 claude_session_id：失败不落，重试从干净会话开始（首轮）或续原会话（后续）。
        if result.session_id and result.session_id != existing_sid:
            await self._update(claude_session_id=result.session_id)

        await self._forward_output(result.text_output)
        await self._set_state(SessionState.CHAT_AWAITING)

    async def _forward_output(self, text: str) -> None:
        """把 claude 整段输出按 ~4000 字分段回发，仅最后一段追加 session tag。"""
        body = (text or "").strip()
        chunks = split_for_chat(body, DEFAULT_CHAT_CHUNK_LIMIT) if body else []
        chunks = [c for c in chunks if c] or [_EMPTY_OUTPUT_NOTICE]
        tag = self._tag()
        last = len(chunks) - 1
        for i, chunk in enumerate(chunks):
            await self._notify(chunk + tag if i == last else chunk)

    # ---------- 续聊 / 取消 ----------

    async def apply_user_message(self, text: str, quote_text: str | None = None) -> bool:
        """把用户后续输入注入下一轮。返回 True 表示已就绪可 drive。

        CHATTING（上一轮在跑）→ False（拒绝，调用方回"正在处理"）；CHAT_AWAITING 或可恢复
        终态（FAILED/TIMEOUT）→ 落 in 消息、set pending、转 CHATTING。
        """
        await self._refresh_row()
        state = SessionState(self.row["state"])
        if state == SessionState.CHATTING:
            return False
        await self.db.add_message(self.slug, "in", text, quote_text=quote_text)
        self.pending_user_message = text
        await self._set_state(SessionState.CHATTING)
        return True

    async def cancel(self, reason: str = "用户取消") -> None:
        await self._set_state(SessionState.CANCELLED, last_error=reason)
        # force=True：DB 已 CANCELLED，_notify 的抑制守卫会吞掉非 force 通知，
        # 而这条回执是 /cancel 路径下唯一的用户反馈。
        await self._notify(f"chat 会话已取消：{reason}", force=True)

    # ---------- 内部工具 ----------

    def _session_dir(self) -> Path:
        return (self.config.workspace_root / "sessions" / self.slug).expanduser()

    def _tag(self) -> str:
        s = self.row.get("display_slug") or self.slug
        repo = self.row.get("repo")
        return "\n\n" + format_session_tag(
            s,
            repo=repo if repo and repo != _NO_REPO else None,
            claude_session_id=self.row.get("claude_session_id"),
        )

    async def _persist_event(self, evt: dict) -> None:
        etype = evt.get("type") or "unknown"
        await self.db.add_event(self.slug, f"claude.{etype}", evt)

    async def _refresh_row(self) -> None:
        row = await self.db.get_session(self.slug)
        if row is None:
            raise RuntimeError(f"chat {self.slug} 不存在于 db")
        self.row = row

    async def _update(self, **fields: Any) -> None:
        await self.db.update_session(self.slug, **fields)
        await self._refresh_row()

    async def _set_state(self, state: SessionState, **extra: Any) -> None:
        """写 DB state + 落 event。带 CANCELLED 吸收守卫（与 Session._set_state 对称）。

        /cancel 是软取消——不 kill 正在跑的 claude；若不设防，run_turn 末尾的
        ``_set_state(CHAT_AWAITING)`` 会把 CANCELLED 覆盖回去，导致会话被误判为仍 open。
        """
        await self._refresh_row()
        current = SessionState(self.row["state"])
        if current == SessionState.CANCELLED and state != SessionState.CANCELLED:
            logger.info(
                "chat %s 已 CANCELLED，忽略 _set_state(%s)", self.slug, state.value
            )
            return
        await self.db.update_session(self.slug, state=state.value, **extra)
        await self.db.add_event(self.slug, "state", {"to": state.value, **extra})
        await self._refresh_row()

    async def _notify(self, text: str, *, force: bool = False) -> None:
        """对用户发出站消息的汇聚点。带 CANCELLED 抑制守卫（与 _set_state 对称）。"""
        if not force:
            await self._refresh_row()
            if SessionState(self.row["state"]) == SessionState.CANCELLED:
                logger.info("chat %s 已 CANCELLED，抑制通知：%s", self.slug, text[:40])
                return
        chatid = self.row.get("chatid") or ""
        await self.db.add_message(self.slug, "out", text)
        await self.reply(chatid, text)
