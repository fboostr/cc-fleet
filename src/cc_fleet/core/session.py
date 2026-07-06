"""Session 状态机：单个需求的完整生命周期驱动。

外部依赖（构造器注入，方便单测 mock）：
- db: storage.Database
- config: AppConfig
- repo_cfg: RepoConfig（本 session 绑定的仓库）
- reply: async (chatid, text) -> None
- runner / reviewer_runner: AgentRunner（默认由 runner_factory.get_runner 按 repo_cfg.agent
  选；测试可经 runner= 注入，或经 claude_run= 注入一个 callable 由 CallableRunner 适配）

工作流：
    NEW → PLANNING ↔ AWAITING_USER_CLARIFICATION → DEVELOPING → MR_SUBMITTING → COMPLETED
                ↘                                          ↘                  ↘
                  FAILED / TIMEOUT                          FAILED / TIMEOUT    FAILED

COMPLETED / FAILED / TIMEOUT 都属于"已结案但可唤醒"终态（``RESUMABLE_TERMINAL_STATES``），
用户引用 bot 回执回复仍能让 session 续推（见 ``apply_followup``）。CANCELLED 是唯一
不可恢复的终态——引用回复按发新需求对待。

引用回复唤醒（apply_followup）按 failed_phase 决定 resume 目标：
- failed_phase=planning → PLANNING（复用澄清路径）
- failed_phase=developing → DEVELOPING（pending_user_message 注入 dev prompt）
- failed_phase=mr_submitting → DEVELOPING（mr 阶段不调 claude，回 dev 兜底）
- failed_phase=new → 拒绝（环境未起，无法续）
- COMPLETED（无 failed_phase）→ DEVELOPING（在已有 worktree 上把 followup 注入为
  "用户对上一轮开发结果的追加反馈" 继续 dev；不重做 plan）

COMPLETED 之后的追加诉求按操作型 followup 处理（"解决冲突 / 补一行 / 微调"），
plan 阶段强协议（要求 ``SLUG:`` / ``STATUS:`` 输出且禁止写文件）与"直接干活"的
followup 语义互斥，因此默认绕过 plan。如需重定方向，建议 @<repo> 开新 session。

启用独立 Reviewer 的 repo（``repo_cfg.reviewer.enabled``）会在 PLANNING 与 DEVELOPING
之后各插入一个审查检查点：

    PLANNING →(READY)→ PLAN_REVIEWING →(NEEDS_REVISION)→ PLANNING（Coder 据意见完善，计 rounds）
                                      ↘(APPROVED)→ PLANNING（Coder 据可选建议再完善一轮，
                                                              下次跳过审查直接进 DEVELOPING）
                                      ↘(审查失败/跳过)→ DEVELOPING
    DEVELOPING →(commit 完成)→ CODE_REVIEWING →(NEEDS_REVISION)→ DEVELOPING（Coder 据意见修订，计 rounds）
                                              ↘(APPROVED)→ DEVELOPING（Coder 据可选建议再修订一轮，
                                                                       下次跳过审查直接进 MR_SUBMITTING）
                                              ↘(审查失败/跳过)→ MR_SUBMITTING

APPROVED 路径也回 PLANNING / DEVELOPING 让 Coder 完善一轮——Reviewer 即便 APPROVED 也常在正文里列
nit/小改进，原来直接放行会丢这些价值。回写时用内存级 ``_skip_next_plan_review`` /
``_skip_next_code_review`` 标记下次跳过审查直接进入下一阶段，避免无限循环；APPROVED 路径**不**累加
``plan_review_rounds`` / ``code_review_rounds``（这两列保留"NEEDS_REVISION 修订循环跑了几次"的语义，
与 ``max_rounds`` 上限对齐）。NEEDS_REVISION 路径完全不动。

Reviewer 是独立 claude 会话（``reviewer_session_id``，绝不 resume Coder 会话），plan 只读模式。
plan 审查 local+remote 都做；code 审查 local+remote 都做（remote 经 defer-push：dev 只 commit，
审查通过后才在 publish 阶段单独 push + 建 MR）。任一审查失败/超时/无法解析 verdict → 跳过、当作没有
Reviewer 一样继续，绝不让 session 进 FAILED。NEEDS_REVISION 的轮次由 ``reviewer.max_rounds`` 限制。

详见 ``core/state.py`` 与 ``apply_followup``/``_resume_target_state``。
"""

from __future__ import annotations

import asyncio
import importlib.resources as resources
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config.schema import AppConfig, RepoConfig
from ..storage.db import Database
from ..util.ids import format_session_tag, new_internal_slug, new_uuid
from . import mr as mr_module
from . import repo as repo_module
from .runner_factory import get_runner
from .runners.base import (
    AgentPermission,
    AgentRunner,
    CallableRunner,
    ClaudeRunResult,
    TimeoutPolicy,
    timeout_phrase,
)
from .runners.claude import (
    LENGTH_ERROR_HINT,
    classify_length_error,
    format_run_failure,
)
from .mr_meta import parse_mr_metadata
from .review import ReviewVerdict, parse_review_output
from .session_log import SessionLogWriter
from .slug import parse_plan_output, resolve_slug_conflict, strip_plan_protocol_tail
from .state import SessionState, is_resumable_terminal

logger = logging.getLogger(__name__)

ReplyFunc = Callable[[str, str], Awaitable[None]]
ClaudeRunFunc = Callable[..., Awaitable[ClaudeRunResult]]


def _prompt_text(name: str) -> Path:
    """返回 prompts/<name> 文件的实际路径。"""
    return Path(resources.files("cc_fleet.prompts").joinpath(name))  # type: ignore[arg-type]


def _prompt_str(name: str) -> str:
    """返回 prompts/<name> 文件的文本内容（供 runner 的 protocol_text 直接注入）。"""
    return _prompt_text(name).read_text(encoding="utf-8")


class Session:
    def __init__(
        self,
        *,
        db: Database,
        config: AppConfig,
        repo_cfg: RepoConfig,
        reply: ReplyFunc,
        runner: AgentRunner | None = None,
        reviewer_runner: AgentRunner | None = None,
        claude_run: ClaudeRunFunc | None = None,
        fetch_lock: asyncio.Lock | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.repo_cfg = repo_cfg
        self.reply = reply
        # 驱动 Coder / Reviewer 的 runner。优先用显式注入的 runner；其次把注入的
        # claude_run（callable，测试用）适配成 CallableRunner；最后按 repo_cfg.agent 经
        # 工厂选。P1 reviewer 跟随 coder 工具（reviewer.tool=None → 同 repo.agent）。
        self.runner: AgentRunner = runner or (
            CallableRunner(claude_run) if claude_run else get_runner(repo_cfg.agent, config)
        )
        self.reviewer_runner: AgentRunner = reviewer_runner or (
            CallableRunner(claude_run)
            if claude_run
            else get_runner(repo_cfg.reviewer.tool or repo_cfg.agent, config)
        )
        # 由 SessionManager 注入的 per-repo lock：fetch_default_branch + create_worktree 写
        # `.git/refs/remotes/origin/<default>` 在并发下有 fs 竞争，按 repo 串行规避。
        # None 表示无并发场景（单测、或单仓 + max_concurrent_sessions=1），直接跑。
        self.fetch_lock = fetch_lock

        self.slug: str = ""             # 内部主键
        self.row: dict[str, Any] = {}   # db 行的内存缓存

        # /kill 手动强杀用：set 后 engine 监控循环立即杀进程组、run 返回 killed=True，
        # drive loop 据此落 CANCELLED。整个 session 生命周期共用同一个 event（多轮 run 复用）。
        self._kill_event: asyncio.Event = asyncio.Event()

        self.pending_user_message: str | None = None
        # apply_followup 拒绝时给调用方拿去回包用户的中文提示；成功路径置 None
        self._last_followup_notice: str | None = None
        # local 模式 dev 阶段从 claude 输出抽出的 MR 元数据缓存。仅内存，进程内有效：
        # MR_SUBMITTING 失败 → followup → DEVELOPING 时 claude 会重跑、重新解析，无需持久化。
        self._pending_mr_title: str | None = None
        self._pending_mr_description: str | None = None
        # Reviewer 判 NEEDS_REVISION 或 APPROVED 后，待注入下一轮 Coder（plan/dev）prompt 的审查意见。
        # 仅内存：从 PLAN_REVIEWING→PLANNING 或 CODE_REVIEWING→DEVELOPING 的同一 drive 循环内消费，
        # 与"用户澄清/followup"的 pending_user_message 区分开，互不串味（修订反馈优先级更高）。
        self._pending_revision_feedback: str | None = None
        # 与 _pending_revision_feedback 配对：True 表示这次 feedback 来自 APPROVED 路径（Reviewer 通过
        # 但仍列了可选建议），下一轮 Coder 的修订 prompt 用"通过后微调"语气；False = NEEDS_REVISION 路径
        # 用"被打回修订"语气。读取后即重置，避免串味到下一次 review。
        self._pending_revision_was_approved: bool = False
        # 上一次 Reviewer 失败跳过时的人话原因（如「plan/上下文过长」）。仅内存，由
        # _run_reviewer 在各失败分支写入，供调用方在「审查跳过」通知里点明根因（None=普通失败）。
        self._last_review_skip_reason: str | None = None
        # Reviewer APPROVED 时也让 Coder 据可选建议再完善一轮（捞回 Reviewer 在 APPROVED 正文里
        # 列出的 nit），但下次重新进入 PLAN_REVIEWING / CODE_REVIEWING 时必须跳过审查直接放行——
        # 否则会形成「APPROVED → Coder 微调 → 再 APPROVED → 再微调」的无意义死循环。
        # 仅内存，一次性消费；主控崩溃恢复后重置为 False（最多多花一次审查成本，行为安全）。
        self._skip_next_plan_review: bool = False
        self._skip_next_code_review: bool = False
        # dev 阶段澄清回路：apply_clarification 唤醒 dev-awaiting（clarify_phase=developing）时置 True，
        # 供 _do_developing 顶部把注入 prompt 的措辞从「追加反馈」调成「回答上一轮的待确认问题」。
        # 仅内存、一次性消费：同一 Session 对象在 awaiting park 期间存活，跨进程重启本就无法唤醒 awaiting。
        self._resuming_dev_clarification: bool = False

    # ---------- 公开入口 ----------

    async def create_row(
        self,
        *,
        initial_request: str,
        chatid: str,
        userid: str,
        review_override: bool | None = None,
        claude_session_id: str | None = None,
        origin_chat_slug: str | None = None,
    ) -> None:
        """同步建 db 行 + 落第一条消息 + session_started 事件；不 drive。

        SessionManager 把"入 db"与"开始 drive"拆开：前者在 dispatch 同步路径
        里完成（让用户立即收到 ack），后者交给后台 task 在 acquire semaphore 后跑。

        ``review_override``：单需求级 Reviewer 覆盖（None 跟随 repo 配置 / True 强制开 /
        False 强制关），落 ``review_override`` 列；SQLite 无原生 bool，存 NULL/1/0。

        ``claude_session_id`` / ``origin_chat_slug``：由 /dev handoff 预置——前者复用被转
        入的 /chat 会话 id（``_do_new`` 会保留、``_do_planning`` 首轮据此 --resume 整段讨论），
        后者标记本 row 由哪条 chat 转入。普通新需求两者都为 None，行为与改动前完全一致。
        """
        self.slug = new_internal_slug()
        await self.db.insert_session(
            {
                "slug": self.slug,
                "display_slug": None,
                "repo": self.repo_cfg.name,
                "state": SessionState.NEW.value,
                "claude_session_id": claude_session_id,
                "worktree_path": None,
                "branch": None,
                "default_branch": self.repo_cfg.default_branch,
                "initial_request": initial_request,
                "chatid": chatid,
                "userid": userid,
                "clarify_rounds": 0,
                "last_error": None,
                "mr_url": None,
                "review_override": (
                    None if review_override is None else int(review_override)
                ),
                "origin_chat_slug": origin_chat_slug,
            }
        )
        await self.db.add_message(self.slug, "in", initial_request)
        await self.db.add_event(
            self.slug,
            "session_started",
            {
                "repo": self.repo_cfg.name,
                "mode": self.repo_cfg.mode,
                "remote_ssh_alias": self.repo_cfg.remote_ssh_alias if self.repo_cfg.mode == "remote" else None,
            },
        )
        logger.info(
            "session %s 启动：repo=%s mode=%s",
            self.slug, self.repo_cfg.name, self.repo_cfg.mode,
        )
        await self._refresh_row()

    async def start(self, *, initial_request: str, chatid: str, userid: str) -> None:
        """便利方法（主要给单测用）：建 row + 直接 drive 到 paused/终态。"""
        await self.create_row(initial_request=initial_request, chatid=chatid, userid=userid)
        await self.drive()

    async def resume(self, slug: str) -> None:
        """从已有 slug 加载 row，准备继续驱动。"""
        self.slug = slug
        await self._refresh_row()

    async def apply_clarification(self, text: str, quote_text: str | None = None) -> bool:
        """同步部分：入 in 消息、设 pending、按 clarify_phase 转回工作态。返回 True 表示已就绪可 drive。

        SessionManager 在 dispatch 路径里调本方法（拿 db 行的最新 state，
        防止 awaiting 假设过期），然后通过 resume_event 唤醒后台 task 继续 drive。

        resume 目标按进入 awaiting 时写下的 ``clarify_phase`` 决定：'developing' → DEVELOPING
        （dev 阶段澄清，复用 dev 的 pending_user_message 注入），其余（'planning' / 老 row 的
        NULL）→ PLANNING，向后兼容。
        """
        await self._refresh_row()
        if self.row.get("state") != SessionState.AWAITING_USER_CLARIFICATION.value:
            logger.warning("session %s 不在 awaiting 状态，忽略澄清回复", self.slug)
            return False
        await self.db.add_message(self.slug, "in", text, quote_text=quote_text)
        self.pending_user_message = text
        if self.row.get("clarify_phase") == SessionState.DEVELOPING.value:
            self._resuming_dev_clarification = True
            await self._set_state(SessionState.DEVELOPING)
        else:
            await self._set_state(SessionState.PLANNING)
        return True

    async def handle_user_clarification(self, text: str, quote_text: str | None = None) -> None:
        """便利方法（主要给单测用）：apply + drive 一气呵成。"""
        if await self.apply_clarification(text, quote_text):
            await self.drive()

    async def apply_followup(self, text: str, quote_text: str | None = None) -> bool:
        """同步部分：对已结案但可恢复的 session（failed/timeout/completed）落消息、切回工作态。

        返回 True 表示 row 已就绪可让后台 task 继续 drive；False 表示拒绝（状态不对、
        worktree 丢失、或失败阶段为 new 无法续）。

        SessionManager 收到 True 后负责起新的后台 task（旧 task 已在 finally 里清退）；
        收到 False 后通过 ``_last_followup_notice`` 拿到给用户的中文提示再回包。
        """
        self._last_followup_notice = None
        await self._refresh_row()
        state = SessionState(self.row["state"])
        if not is_resumable_terminal(state):
            logger.warning("session %s 不在可恢复终态（state=%s），忽略 follow-up", self.slug, state)
            return False

        target = self._resume_target_state()
        if target is None:
            self._last_followup_notice = (
                "session 环境创建阶段就失败了，无法继续。请 @<repo> 重新发起需求。"
            )
            return False

        if not self._worktree_intact():
            self._last_followup_notice = (
                f"session [{self.row.get('display_slug') or self.slug}] 的 worktree 已丢失，"
                "无法继续。请 @<repo> 重新发起需求。"
            )
            return False

        await self.db.add_message(self.slug, "in", text, quote_text=quote_text)
        self.pending_user_message = text
        await self._set_state(target)
        return True

    async def handle_user_followup(self, text: str, quote_text: str | None = None) -> None:
        """便利方法（主要给单测用）：apply_followup + drive 一气呵成。"""
        if await self.apply_followup(text, quote_text):
            await self.drive()

    async def cancel(self, reason: str = "用户取消") -> None:
        await self._set_state(SessionState.CANCELLED, last_error=reason)
        # force=True 必需：此时 DB 已是 CANCELLED，_notify 的抑制守卫会吞掉非 force
        # 通知；而这条回执在 CLI 取消路径（cli.py→mgr.cancel）下是唯一的用户回执。
        await self._notify(f"session 已取消：{reason}", force=True)

    def hard_kill(self) -> None:
        """``/kill`` 手动强杀：set ``kill_event``，engine 监控循环下一轮（≤1s）杀活进程组、
        ``run`` 返回 ``killed=True``，调用点据此直接收尾。落 CANCELLED + 回执由
        ``SessionManager.hard_cancel`` 复用 ``cancel()`` 完成（与软取消同路径）。

        与 ``cancel()``（软取消，不杀活进程、等 claude 自己跑完）互补：``/kill`` 面向
        「claude 卡死 / 跑飞、不想再等」的场景。同步方法：只按信号，不落库。"""
        self._kill_event.set()

    # ---------- 状态机主循环 ----------

    async def drive(self) -> None:
        """从当前状态推进，直到 paused（awaiting）或终态。"""
        while True:
            state = SessionState(self.row["state"])
            logger.info("session %s 进入 %s", self.slug, state.value)
            if state == SessionState.NEW:
                await self._do_new()
            elif state == SessionState.PLANNING:
                await self._do_planning()
            elif state == SessionState.PLAN_REVIEWING:
                await self._do_plan_reviewing()
            elif state == SessionState.DEVELOPING:
                await self._do_developing()
            elif state == SessionState.CODE_REVIEWING:
                await self._do_code_reviewing()
            elif state == SessionState.MR_SUBMITTING:
                await self._do_mr_submitting()
            else:
                # awaiting / 终态 — 退出循环
                break

    # ---------- 各状态的 action ----------

    async def _do_new(self) -> None:
        session_dir = self._session_dir()
        session_dir.mkdir(parents=True, exist_ok=True)

        if self._is_remote():
            # 远端模式：本地不创建 worktree、不建分支；claude 启动 cwd = 壳子目录。
            # 真正的 worktree 与分支由 claude 在 dev 阶段于远端创建。
            worktree_path = self.repo_cfg.path
            branch: str | None = None
        else:
            # worktree 根目录按约定自动推导：<repo.path>-worktrees
            # 例如 ~/cfd-solver → ~/cfd-solver-worktrees/<slug>
            worktree_root = self.repo_cfg.path.with_name(
                self.repo_cfg.path.name + "-worktrees"
            )
            worktree_path = worktree_root / self.slug
            branch = f"claude/{self.slug}"
            base = f"origin/{self.repo_cfg.default_branch}"
            # 幂等：主控重启 recover NEW session 时 worktree 可能已存在。已建好（目录在 +
            # 含 `.git` 标记）就直接复用，跳过 fetch + create，避免 `git worktree add -b`
            # 因分支已存在而 GitError。
            already_exists = worktree_path.exists() and (worktree_path / ".git").exists()
            if not already_exists:
                try:
                    if self.fetch_lock is not None:
                        async with self.fetch_lock:
                            await repo_module.fetch_default_branch(self.repo_cfg.path, self.repo_cfg.default_branch)
                            await repo_module.create_worktree(self.repo_cfg.path, worktree_path, branch, base)
                    else:
                        await repo_module.fetch_default_branch(self.repo_cfg.path, self.repo_cfg.default_branch)
                        await repo_module.create_worktree(self.repo_cfg.path, worktree_path, branch, base)
                except repo_module.GitError as e:
                    await self._fail(f"创建 worktree 失败:{e}")
                    return

        # 保留已分配的 claude_session_id：NEW 阶段中途崩溃 recover 时 sid 可能已有，
        # 重新生成会丢 claude SDK 端会话上下文（resume_from 续会话依赖此 id 不变）。
        sid = self.row.get("claude_session_id") or new_uuid()
        await self._update(
            worktree_path=str(worktree_path),
            branch=branch,
            claude_session_id=sid,
        )
        await self._set_state(SessionState.PLANNING)

    async def _do_planning(self) -> None:
        worktree = Path(self.row["worktree_path"])
        guardrail = self.runner.guardrail.prepare(settings_dir=self._session_dir() / ".cc-fleet")
        stream_log = self._session_dir() / "stream.jsonl"

        resumed_handoff = False
        # prompt 优先级：Reviewer 修订反馈 > 用户澄清回路 > 首次 plan
        if self._pending_revision_feedback is not None:
            # 从 PLAN_REVIEWING 回来：把 Reviewer 意见作为修订指令，resume Coder 会话完善 plan。
            # approved 由配对标志 _pending_revision_was_approved 决定 prompt 语气；同时消费并清零。
            prompt = self._format_plan_revision_prompt(
                self._pending_revision_feedback,
                approved=self._pending_revision_was_approved,
            )
            self._pending_revision_feedback = None
            self._pending_revision_was_approved = False
            resume_from = self.row["claude_session_id"]
        else:
            # 首次 plan vs 澄清回路
            is_first = self.row["clarify_rounds"] == 0 and not self.pending_user_message
            if is_first:
                if self.row.get("origin_chat_slug"):
                    # /dev handoff：首轮 --resume 被转入的 /chat 会话（claude_session_id 已由
                    # create_row 预置、_do_new 保留），基于讨论直接进入规划，不重述需求。
                    prompt = self._format_handoff_plan_prompt()
                    resume_from = self.row["claude_session_id"]
                    resumed_handoff = True
                else:
                    prompt = self.row["initial_request"]
                    resume_from = None
            else:
                prompt = self.pending_user_message or ""
                self.pending_user_message = None
                resume_from = self.row["claude_session_id"]

        result = await self.runner.run(
            prompt=prompt,
            cwd=worktree,
            permission=AgentPermission.READ_ONLY,
            protocol_text=_prompt_str("plan_protocol.md"),
            session_id=self.row["claude_session_id"],
            resume_from=resume_from,
            guardrail=guardrail,
            timeout=self.config.stage_timeout(self.repo_cfg, "plan").to_policy(),
            stream_log_path=stream_log,
            extra_env=self._claude_extra_env(worktree),
            on_event=self._persist_claude_event,
            kill_event=self._kill_event,
        )

        if result.killed:
            # /kill 强杀：hard_cancel 已落 CANCELLED（_set_state 守卫会吸收后续写入），
            # 直接收尾，勿覆盖成 TIMEOUT。
            return
        if result.timed_out:
            reason = f"plan 阶段{timeout_phrase(result.timeout_kind)}超时"
            await self._timeout(reason)
            await self._notify(f"{reason}{self._tag()}")
            return
        if result.exit_code not in (0, None) or result.result_is_error:
            await self._fail(format_run_failure(result, "plan"))
            return

        # /dev handoff 首轮 --resume 的是外部（chat）建的会话，claude 可能 fork 出新 id；
        # 采纳它，否则后续轮会 resume 到陈旧 id。普通 pipeline 首轮走 --session-id 不受影响，
        # 故以 resumed_handoff 守卫，对普通路径零副作用（与 chat.py:run_turn 同类处理对称）。
        if (
            resumed_handoff
            and result.session_id
            and result.session_id != self.row["claude_session_id"]
        ):
            await self._update(claude_session_id=result.session_id)

        protocol = parse_plan_output(result.text_output)
        if protocol.status is None:
            await self._fail("plan 阶段未按协议输出 STATUS 字段，无法继续。")
            return

        # 首次拿到 slug，做冲突解决并存 display_slug
        if self.row.get("display_slug") is None and protocol.slug:
            display = await resolve_slug_conflict(protocol.slug, self.db.display_slug_exists)
            await self._update(display_slug=display)

        # 把 plan 正文（剥协议尾）落到 sessions/<slug>/plan.md，覆盖上一轮
        plan_path = self._write_plan_md(result.text_output)

        if protocol.status == "READY":
            if self._plan_review_enabled():
                # 交独立 Reviewer 审查；通知放在 _do_plan_reviewing 内发，
                # 避免此处先发"开始开发"误导（实际还要先过审查）。
                await self._set_state(SessionState.PLAN_REVIEWING)
            else:
                await self._notify_plan_ready(plan_path)
                await self._set_state(SessionState.DEVELOPING)
        else:  # NEED_CLARIFICATION
            if self.row["clarify_rounds"] >= self.config.pipeline.max_clarify_rounds:
                await self._fail(
                    f"澄清轮数已超过上限 {self.config.pipeline.max_clarify_rounds}"
                )
                return
            await self._update(clarify_rounds=self.row["clarify_rounds"] + 1)
            await self._set_state(
                SessionState.AWAITING_USER_CLARIFICATION,
                clarify_phase=SessionState.PLANNING.value,
            )
            await self._notify_clarification(protocol.questions, plan_path)

    async def _do_developing(self) -> None:
        worktree = Path(self.row["worktree_path"])
        guardrail = self.runner.guardrail.prepare(settings_dir=self._session_dir() / ".cc-fleet")
        stream_log = self._session_dir() / "stream.jsonl"
        dev_protocol_text = self._render_dev_system_prompt()

        followup = self.pending_user_message
        self.pending_user_message = None
        # dev 澄清回路标志一次性消费（无论下面走哪个分支都复位，避免泄漏到后续 dev 轮）
        resuming_dev_clarification = self._resuming_dev_clarification
        self._resuming_dev_clarification = False
        if self._pending_revision_feedback is not None:
            # 从 CODE_REVIEWING 回来：把 Reviewer 代码审查意见作为修订指令注入。
            # approved 由配对标志 _pending_revision_was_approved 决定 prompt 语气；同时消费并清零。
            prompt = self._format_code_revision_prompt(
                self._pending_revision_feedback,
                approved=self._pending_revision_was_approved,
            )
            self._pending_revision_feedback = None
            self._pending_revision_was_approved = False
        elif followup and resuming_dev_clarification:
            # dev 澄清回路：上一轮 dev 输出 NEED_CLARIFICATION 挂起等用户，这里注入用户答复继续开发
            prompt = (
                f"针对你上一轮提出的待确认问题，用户答复如下：\n{followup}\n\n"
                "请据此继续开发；其它规则见已注入的 system prompt。\n"
                "若仍需用户决策，按 dev 协议输出 STATUS: NEED_CLARIFICATION + QUESTIONS。"
            )
        elif followup:
            # 引用回复唤醒（failed/timeout/completed → DEVELOPING）时把用户消息注入 prompt
            prompt = (
                f"用户对上一轮开发结果的追加反馈：\n{followup}\n\n"
                "请按以上反馈继续开发；其它规则见已注入的 system prompt。\n"
                "若遇阻塞，请在回复中明确说明，并附上原始命令与原始报错。"
            )
        else:
            prompt = (
                "现在按上一阶段的 plan 完成开发；具体规则见已注入的 system prompt。\n"
                "若遇阻塞，请在回复中明确说明，并附上原始命令与原始报错。"
            )

        result = await self.runner.run(
            prompt=prompt,
            cwd=worktree,
            permission=AgentPermission.WRITE,
            protocol_text=dev_protocol_text,
            session_id=self.row["claude_session_id"],
            resume_from=self.row["claude_session_id"],
            guardrail=guardrail,
            timeout=self.config.stage_timeout(self.repo_cfg, "dev").to_policy(),
            stream_log_path=stream_log,
            extra_env=self._claude_extra_env(worktree),
            on_event=self._persist_claude_event,
            kill_event=self._kill_event,
        )

        if result.killed:
            return  # /kill 强杀：hard_cancel 已落 CANCELLED，直接收尾
        if result.timed_out:
            reason = f"dev 阶段{timeout_phrase(result.timeout_kind)}超时"
            await self._timeout(reason)
            await self._notify(f"开发{timeout_phrase(result.timeout_kind)}超时{self._tag()}")
            return
        if result.exit_code not in (0, None) or result.result_is_error:
            await self._fail(format_run_failure(result, "dev"))
            return

        # commit 闸门之前先看 claude 是否请求用户决策（dev 澄清协议，与 plan 阶段同 STATUS/QUESTIONS
        # 语法、复用 parse_plan_output）。命中则挂 awaiting 等用户回复，而非因「无新 commit」误判失败。
        # 放在 local/remote 分叉之前 → 两模式共用；正常 dev 输出无 STATUS 行时 status=None，零干扰；
        # 即便 claude 既 commit 又提问也以澄清优先（残留 commit 保留在 worktree，答复后 resume 续跑）。
        clarify = parse_plan_output(result.text_output)
        if clarify.status == "NEED_CLARIFICATION":
            if self.row["clarify_rounds"] >= self.config.pipeline.max_clarify_rounds:
                await self._fail(
                    f"澄清轮数已超过上限 {self.config.pipeline.max_clarify_rounds}"
                )
                return
            await self._update(clarify_rounds=self.row["clarify_rounds"] + 1)
            await self._set_state(
                SessionState.AWAITING_USER_CLARIFICATION,
                clarify_phase=SessionState.DEVELOPING.value,
            )
            await self._notify_clarification(clarify.questions)
            return

        # dev 阶段两种模式都只到 commit 为止（remote defer-push 后也不再在 dev 内 push/建 MR），
        # 闸门：确认确有新 commit（local 直接查本地 worktree，remote 经 SSH 查远端 worktree）。
        base = f"origin/{self.row['default_branch']}"
        if self._is_remote():
            remote_worktree = self._remote_worktree_path()
            try:
                ahead = await repo_module.has_commits_ahead_remote(
                    self.repo_cfg.remote_ssh_alias or "", remote_worktree, base
                )
            except Exception as e:  # noqa: BLE001
                await self._fail(f"检查远端 worktree 提交状态失败：{e}")
                return
            if not ahead:
                await self._fail("dev 阶段结束但远端 worktree 无新 commit。")
                return
            # 远端 MR 元数据由 publish 阶段 claude 产出，主控此处不解析。
        else:
            # 本地模式：claude 应该已经 commit；主控负责 push + 提 MR
            try:
                ahead = await repo_module.has_commits_ahead(worktree, base)
            except Exception as e:  # noqa: BLE001
                await self._fail(f"检查 worktree 提交状态失败：{e}")
                return
            if not ahead:
                await self._fail("dev 阶段结束但 worktree 无新 commit。")
                return

            # 从 claude 输出抽 MR 元数据缓存到 self；MR_SUBMITTING 阶段优先用，
            # 缺失则在 _mr_title / _mr_description 内走 git log 兜底。
            meta = parse_mr_metadata(result.text_output)
            self._pending_mr_title = meta.title
            self._pending_mr_description = meta.description
            if meta.title is None or meta.description is None:
                logger.warning(
                    "session %s dev 阶段未按协议输出完整 MR 元数据（title=%s description=%s），"
                    "将走 git log 兜底",
                    self.slug,
                    "ok" if meta.title else "missing",
                    "ok" if meta.description else "missing",
                )

        # 两模式统一：commit 完成后，开了审查就先审代码，否则直接进发布
        if self._code_review_enabled():
            # 发布前先交独立 Reviewer 审查代码；通知在 _do_code_reviewing 内发
            await self._set_state(SessionState.CODE_REVIEWING)
        else:
            await self._set_state(SessionState.MR_SUBMITTING)

    async def _do_mr_submitting(self) -> None:
        # remote：发布由 claude 经 SSH 完成（push + 建 MR/PR）；local：主控直接提 MR。
        if self._is_remote():
            await self._do_publish_remote()
            return
        title = await self._mr_title()
        description = await self._mr_description()
        worktree = Path(self.row["worktree_path"])
        platform = await self._resolve_platform(worktree)
        try:
            mr_url = await mr_module.create_review_request(
                platform=platform,
                worktree=worktree,
                source_branch=self.row["branch"],
                target_branch=self.row["default_branch"],
                title=title,
                description=description,
            )
        except mr_module.MrCreateError as e:
            await self._fail(f"MR 提交失败：{e}")
            return

        await self._update(mr_url=mr_url)
        await self._set_state(SessionState.COMPLETED)
        await self._notify(
            f"开发完成 ✅\nMR：{mr_url}{self._tag()}"
        )

    async def _do_publish_remote(self) -> None:
        """远端发布阶段（defer-push）：resume Coder 会话，按 publish 协议在远端 push + 建 MR/PR，
        主控从输出抽 `MR_URL:`。镜像旧 dev-remote 的 MR_URL 处理，只是挪到审查之后单独成阶段。"""
        await self._notify(f"代码就绪，发布中（push + 建 MR/PR）…{self._tag()}")
        worktree = Path(self.row["worktree_path"])  # 壳子目录（cwd）
        guardrail = self.runner.guardrail.prepare(settings_dir=self._session_dir() / ".cc-fleet")
        stream_log = self._session_dir() / "stream.jsonl"
        publish_protocol_text = self._render_publish_system_prompt()
        prompt = (
            "代码已通过审查（或本 session 未启用代码审查）。现在按已注入的 system prompt 执行发布："
            "在远端 worktree push 分支并创建 MR/PR，最后按协议另起一行输出 `MR_URL:`。\n"
            "若遇阻塞，请在回复中明确说明，并附上原始命令与原始报错。"
        )
        result = await self.runner.run(
            prompt=prompt,
            cwd=worktree,
            permission=AgentPermission.WRITE,
            protocol_text=publish_protocol_text,
            session_id=self.row["claude_session_id"],
            resume_from=self.row["claude_session_id"],
            guardrail=guardrail,
            timeout=self.config.stage_timeout(self.repo_cfg, "dev").to_policy(),
            stream_log_path=stream_log,
            extra_env=self._claude_extra_env(worktree),
            on_event=self._persist_claude_event,
            kill_event=self._kill_event,
        )

        if result.killed:
            return  # /kill 强杀：hard_cancel 已落 CANCELLED，直接收尾
        if result.timed_out:
            reason = f"发布阶段{timeout_phrase(result.timeout_kind)}超时"
            await self._timeout(reason)
            await self._notify(f"发布{timeout_phrase(result.timeout_kind)}超时{self._tag()}")
            return
        if result.exit_code not in (0, None) or result.result_is_error:
            await self._fail(format_run_failure(result, "publish"))
            return

        mr_url = mr_module.extract_mr_url_from_text(result.text_output)
        if not mr_url:
            tail = result.text_output[-500:] if result.text_output else ""
            await self._fail(
                "发布阶段完成但未输出 `MR_URL:` 协议行。"
                "请检查 claude 是否成功 push 并能从 git stderr / API 响应拿到 MR/PR URL。\n"
                f"输出末尾：{tail}"
            )
            return

        await self._update(mr_url=mr_url)
        await self._set_state(SessionState.COMPLETED)
        await self._notify(f"开发完成 ✅\nMR：{mr_url}{self._tag()}")

    # ---------- Reviewer（独立审查 agent） ----------

    def _reviewer_enabled_effective(self) -> bool:
        """本 session 是否启用 Reviewer：单需求级 ``review_override`` 优先，回退 repo 配置。

        ``review_override`` 列：None=不覆盖（跟随 repo ``reviewer.enabled``）/ 1=强制开 / 0=强制关。
        """
        override = self.row.get("review_override")
        if override is not None:
            return bool(override)
        rc = getattr(self.repo_cfg, "reviewer", None)
        return bool(rc and rc.enabled)

    def _reviewer_max_rounds(self) -> int:
        """「审查→修订」轮次上限：取 repo ``reviewer.max_rounds``（缺省 1）。

        单需求 ``[review]`` 强制开启时至少保证 1 轮，避免 repo 把 ``max_rounds`` 显式设为 0
        反而把"强制开"架空成不审。
        """
        rc = getattr(self.repo_cfg, "reviewer", None)
        rounds = rc.max_rounds if rc else 1
        if self.row.get("review_override"):
            return max(rounds, 1)
        return rounds

    def _plan_review_enabled(self) -> bool:
        """是否对当前 session 做 plan 审查：reviewer 启用且未用尽轮次。local + remote 均可。

        额外的 ``_skip_next_plan_review`` 一次性闸门用于 APPROVED 路径——APPROVED 后 Coder 已据可选
        建议再完善了一轮，下次走到这里必须直接放行，否则会陷入「再 APPROVED → 再微调」死循环。
        闸门**一次性消费**：本次返回 False，同时复位标志，后续若有合法的"再起一轮 plan"诉求
        （如用户引用回复唤醒）仍能正常触发审查。
        """
        if self._skip_next_plan_review:
            self._skip_next_plan_review = False
            return False
        if not self._reviewer_enabled_effective():
            return False
        return (self.row.get("plan_review_rounds") or 0) < self._reviewer_max_rounds()

    def _code_review_enabled(self) -> bool:
        """是否对当前 session 做 code 审查：reviewer 启用且未用尽轮次。local + remote 均可。

        remote 模式经 defer-push 改造后，dev 阶段只 commit 不 push，审查通过才发布（push+建 MR），
        故 code 审查在 remote 也有了插入位置（Reviewer 经 SSH 只读 diff 远端 worktree）。

        ``_skip_next_code_review`` 一次性闸门与 ``_plan_review_enabled`` 同构，用于 code review
        APPROVED 路径下避免「APPROVED → Coder 微调 → 再 APPROVED」死循环。
        """
        if self._skip_next_code_review:
            self._skip_next_code_review = False
            return False
        if not self._reviewer_enabled_effective():
            return False
        return (self.row.get("code_review_rounds") or 0) < self._reviewer_max_rounds()

    async def _do_plan_reviewing(self) -> None:
        """独立 Reviewer 审查 plan。失败/超时/无 verdict → 跳过进开发；NEEDS_REVISION → 回 PLANNING。"""
        await self._notify(f"plan 初稿就绪，独立 Reviewer 审查中…{self._tag()}")
        plan_path = (self._session_dir() / "plan.md").resolve()
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except OSError:
            plan_text = ""
        context = await self._build_requirement_context()
        prompt = (
            "请以独立评审员的身份审查下面这份 plan（审查标准见已注入的 system prompt）。\n\n"
            f"{context}\n\n"
            "## 待审查的 plan\n"
            f"{plan_text}"
        )
        verdict = await self._run_reviewer(
            prompt=prompt,
            protocol_file="plan_review_protocol.md",
            timeout=self.config.stage_timeout(self.repo_cfg, "review").to_policy(),
        )

        if verdict is None:  # 失败即跳过：当作没有 Reviewer
            reason = self._last_review_skip_reason
            if reason:
                await self._notify(
                    f"⚠️ Reviewer 审查跳过：{reason}。已照常继续开发，但本次 plan **未经独立审查**。"
                    f"{LENGTH_ERROR_HINT}{self._tag()}"
                )
            else:
                await self._notify(f"⚠️ Reviewer 审查未完成，已跳过，继续开发。{self._tag()}")
            await self._notify_plan_ready(plan_path)
            await self._set_state(SessionState.DEVELOPING)
            return

        review_path = self._write_review_md(verdict.body, "plan_review.md")
        if verdict.status == "APPROVED":
            # APPROVED 也回 PLANNING 让 Coder 据正文里的可选建议再完善一轮——Reviewer 即便 APPROVED
            # 也常列出 nit/小改进，直接放行会丢这些价值。用 _skip_next_plan_review 一次性闸门确保
            # 下次走 PLAN_REVIEWING 时跳过审查直接进 DEVELOPING，避免「再 APPROVED → 再微调」死循环。
            # APPROVED 路径**不**累加 plan_review_rounds（rounds 保留 NEEDS_REVISION 修订循环计数语义）。
            self._pending_revision_feedback = verdict.body
            self._pending_revision_was_approved = True
            self._skip_next_plan_review = True
            await self._notify(
                f"Reviewer 审查通过 ✅\nCoder 据可选建议最终完善 plan 中…\n"
                f"审查意见：{review_path}{self._tag()}"
            )
            await self._set_state(SessionState.PLANNING)
            return

        # NEEDS_REVISION：意见交回 Coder 完善 plan
        await self._update(plan_review_rounds=(self.row.get("plan_review_rounds") or 0) + 1)
        self._pending_revision_feedback = verdict.body
        self._pending_revision_was_approved = False
        await self._notify(
            f"Reviewer 提出修订意见，Coder 据此完善 plan 中…\n审查意见：{review_path}{self._tag()}"
        )
        await self._set_state(SessionState.PLANNING)

    async def _do_code_reviewing(self) -> None:
        """独立 Reviewer 审查代码。失败/超时/无 verdict → 跳过提 MR；NEEDS_REVISION → 回 DEVELOPING。"""
        await self._notify(f"开发完成，独立 Reviewer 审查代码中…{self._tag()}")
        plan_path = self._session_dir() / "plan.md"
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except OSError:
            plan_text = ""
        base = f"origin/{self.row['default_branch']}"
        context = await self._build_requirement_context()
        if self._is_remote():
            # 远端代码在 dev box，Reviewer 经 SSH 只读查看 diff（worktree 路径确定性拼出）
            diff_cmd = (
                f"ssh {self.repo_cfg.remote_ssh_alias} "
                f"'cd {self._remote_worktree_path()} && git diff {base}'"
            )
            prompt = (
                "请以独立评审员的身份审查本次代码实现（审查标准见已注入的 system prompt）。\n"
                f"先运行 `{diff_cmd}` 查看全部改动（**只读**，勿在远端做任何写操作）。\n\n"
                f"{context}\n\n"
                "## Coder 据以开发的 plan\n"
                f"{plan_text}"
            )
            protocol_file = "code_review_protocol_remote.md"
        else:
            prompt = (
                "请以独立评审员的身份审查本次代码实现（审查标准见已注入的 system prompt）。\n"
                f"先运行 `git diff {base}` 查看全部改动。\n\n"
                f"{context}\n\n"
                "## Coder 据以开发的 plan\n"
                f"{plan_text}"
            )
            protocol_file = "code_review_protocol.md"
        verdict = await self._run_reviewer(
            prompt=prompt,
            protocol_file=protocol_file,
            timeout=self.config.stage_timeout(self.repo_cfg, "review").to_policy(),
        )

        if verdict is None:  # 失败即跳过
            reason = self._last_review_skip_reason
            if reason:
                await self._notify(
                    f"⚠️ Reviewer 代码审查跳过：{reason}。已照常继续提 MR，但本次代码**未经独立审查**。"
                    f"{LENGTH_ERROR_HINT}{self._tag()}"
                )
            else:
                await self._notify(f"⚠️ Reviewer 代码审查未完成，已跳过，继续提 MR。{self._tag()}")
            await self._set_state(SessionState.MR_SUBMITTING)
            return

        review_path = self._write_review_md(verdict.body, "code_review.md")
        if verdict.status == "APPROVED":
            # APPROVED 也回 DEVELOPING 让 Coder 据正文里的可选建议再修订一轮；与 plan APPROVED 同构，
            # _skip_next_code_review 闸门保证下次走 CODE_REVIEWING 时跳过审查直接进 MR_SUBMITTING。
            # APPROVED 路径**不**累加 code_review_rounds（rounds 保留 NEEDS_REVISION 修订循环计数语义）。
            self._pending_revision_feedback = verdict.body
            self._pending_revision_was_approved = True
            self._skip_next_code_review = True
            await self._notify(
                f"Reviewer 代码审查通过 ✅\nCoder 据可选建议最终调整代码中…\n"
                f"审查意见：{review_path}{self._tag()}"
            )
            await self._set_state(SessionState.DEVELOPING)
            return

        # NEEDS_REVISION：意见交回 Coder 修订实现
        await self._update(code_review_rounds=(self.row.get("code_review_rounds") or 0) + 1)
        self._pending_revision_feedback = verdict.body
        self._pending_revision_was_approved = False
        await self._notify(
            f"Reviewer 提出代码修订意见，Coder 据此修改中…\n审查意见：{review_path}{self._tag()}"
        )
        await self._set_state(SessionState.DEVELOPING)

    async def _run_reviewer(
        self, *, prompt: str, protocol_file: str, timeout: TimeoutPolicy
    ) -> ReviewVerdict | None:
        """跑一次独立 Reviewer（plan 只读模式，独立会话）。

        返回 ``ReviewVerdict``（含 status + 剥协议尾的 body）；任何失败（异常/超时/退出码
        非 0/result is_error/无法解析 verdict）一律返回 None，由调用方按「失败即跳过」处理。
        Reviewer 用独立 ``reviewer_session_id``，绝不 resume Coder 的 claude 会话；首次审查
        成功后持久化该 id，后续审查 resume 它以保持上下文连续（plan 审查→code 审查）。
        """
        worktree = Path(self.row["worktree_path"])
        guardrail = self.reviewer_runner.guardrail.prepare(settings_dir=self._session_dir() / ".cc-fleet")
        stream_log = self._session_dir() / "reviewer_stream.jsonl"
        self._last_review_skip_reason = None  # 每轮重置，避免上一次的原因串味

        existing_sid = self.row.get("reviewer_session_id")
        if existing_sid:
            call_sid, resume_from = existing_sid, existing_sid
        else:
            call_sid, resume_from = new_uuid(), None

        try:
            result = await self.reviewer_runner.run(
                prompt=prompt,
                cwd=worktree,
                permission=AgentPermission.READ_ONLY,
                protocol_text=_prompt_str(protocol_file),
                session_id=call_sid,
                resume_from=resume_from,
                guardrail=guardrail,
                timeout=timeout,
                stream_log_path=stream_log,
                extra_env=self._claude_extra_env(worktree),
                on_event=self._persist_reviewer_event,
                kill_event=self._kill_event,
            )
        except Exception as e:  # noqa: BLE001 - Reviewer 失败一律降级跳过，绝不拖垮 session
            logger.warning("session %s Reviewer 调用异常，跳过审查：%s", self.slug, e)
            self._last_review_skip_reason = classify_length_error(e)
            return None

        if result.timed_out or result.killed:
            # 超时或被 /kill 强杀：Reviewer 失败即跳过（当作没有 Reviewer）。killed 时
            # session 已被 hard_cancel 落 CANCELLED，返回 None 后调用点的 _set_state 也被守卫吸收。
            logger.warning("session %s Reviewer 审查中断（超时/强杀），跳过", self.slug)
            return None
        if result.exit_code not in (0, None) or result.result_is_error:
            logger.warning(
                "session %s Reviewer 审查失败（exit=%s is_error=%s），跳过",
                self.slug, result.exit_code, result.result_is_error,
            )
            self._last_review_skip_reason = classify_length_error(result)
            return None

        verdict = parse_review_output(result.text_output)
        if verdict.status is None:
            logger.warning("session %s Reviewer 未按协议输出 REVIEW_VERDICT，跳过", self.slug)
            return None

        # 首次审查成功才持久化 reviewer 会话 id（避免早失败留下无法 resume 的幽灵会话 id）
        if not existing_sid:
            await self._update(reviewer_session_id=result.session_id or call_sid)
        return verdict

    async def _persist_reviewer_event(self, evt: dict) -> None:
        """把 Reviewer claude 的每条事件落到 events 表，kind 加 ``reviewer.`` 前缀（与 Coder 的
        ``claude.`` 前缀区分，便于前端/排查时分辨是哪个 agent 的事件）。"""
        etype = evt.get("type") or "unknown"
        await self.db.add_event(self.slug, f"reviewer.{etype}", evt)

    async def _build_requirement_context(self) -> str:
        """拼装喂给 Reviewer 的需求上下文：原始需求 + plan 阶段澄清问答来回。

        Reviewer 是独立会话，没有 Coder 的上下文；这里把用户原始需求与澄清记录补给它，
        让审查能对照「用户到底要什么 / 拍板过什么」。澄清记录取 db messages（首条 in 是
        原始需求，已单列故跳过）。
        """
        request = (self.row.get("initial_request") or "").strip()
        parts = [f"## 用户原始需求\n{request or '（无）'}"]
        try:
            msgs = await self.db.list_messages(self.slug)
        except Exception:  # noqa: BLE001
            msgs = []
        clar: list[str] = []
        for m in msgs[1:]:
            text = (m.get("text") or "").strip()
            if not text:
                continue
            who = "用户" if m.get("direction") == "in" else "机器人"
            clar.append(f"- {who}：{text}")
        if clar:
            parts.append("## plan 阶段澄清问答\n" + "\n".join(clar))
        return "\n\n".join(parts)

    def _format_handoff_plan_prompt(self) -> str:
        """/dev handoff 首轮 plan prompt：基于已 --resume 的 /chat 讨论直接规划。

        关键是提示 claude **不要**把需求重问一遍 / 从零发散——讨论已在被 resume 的会话
        上下文里。附上转入时组装的 initial_request（含对话最初需求 + 转开发补充说明）兜底。
        """
        req = (self.row.get("initial_request") or "").strip()
        return (
            "我们刚才已经在对话中把这个需求讨论清楚了。现在进入正式的规划（plan）阶段：\n"
            "请**基于我们前面对话中已经达成的理解与结论**产出实施 plan，不要把需求重新问一遍，"
            "也不要从零重新发散。\n\n"
            f"{req}\n\n"
            "其余输出要求见已注入的 plan 协议（正文给出 plan，末尾按格式输出 SLUG / STATUS）。"
        )

    def _format_plan_revision_prompt(self, feedback: str, *, approved: bool = False) -> str:
        if approved:
            intro = (
                "独立 Reviewer **已审查通过**你刚才的 plan（REVIEW_VERDICT: APPROVED），"
                "但在正文中仍列出了一些可选的小改进 / nit。请**逐条评估**："
                "合理的就据此完善 plan，可忽略的请在 plan 中简单说明理由（不必为凑改动硬采纳）。"
            )
        else:
            intro = (
                "独立 Reviewer 对你刚才的 plan 给出了以下审查意见。请**逐条评估**："
                "认同的就据此完善 plan，不认同的请在 plan 里说明理由。"
            )
        return (
            intro
            + "完善后**重新按协议输出**完整 plan，以及末尾的 SLUG / STATUS 字段。\n"
            + "若审查意见暴露出需要用户拍板的真实歧义，可输出 STATUS: NEED_CLARIFICATION 提问。\n\n"
            + f"===== Reviewer 审查意见 =====\n{feedback}"
        )

    def _format_code_revision_prompt(self, feedback: str, *, approved: bool = False) -> str:
        if self._is_remote():
            # remote defer-push：dev 阶段不 push、不建 MR；修订后继续 commit，发布仍留到审查通过后
            action = (
                "认同的就在远端 worktree 修改代码并重新 `git commit`，不认同的请在回复中说明理由。"
                "**仍不要 push、不要建 MR**——发布会在审查通过后单独进行。\n"
            )
        else:
            action = (
                "认同的就修改代码并重新 `git commit`，不认同的请在回复中说明理由。"
                "完成后**重新按协议输出** MR 元数据（MR_TITLE / MR_DESCRIPTION）。\n"
            )
        if approved:
            intro = (
                "独立 Reviewer **已审查通过**你刚才的代码实现（REVIEW_VERDICT: APPROVED），"
                "但在正文中仍列出了一些可选的小改进 / nit。请**逐条评估**："
            )
        else:
            intro = (
                "独立 Reviewer 对你刚才的代码实现给出了以下审查意见。请**逐条评估**："
            )
        return (
            intro
            + action
            + "若遇阻塞，请在回复中明确说明，并附上原始命令与原始报错。\n\n"
            + f"===== Reviewer 审查意见 =====\n{feedback}"
        )

    def _write_review_md(self, body: str, filename: str) -> Path:
        """把审查正文（已剥协议尾）落到 sessions/<slug>/<filename>，返回绝对路径。"""
        path = self._session_dir() / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path.resolve()

    # ---------- 辅助 ----------

    def _session_dir(self) -> Path:
        return (self.config.workspace_root / "sessions" / self.slug).expanduser()

    def _session_log(self) -> SessionLogWriter:
        """惰性构造 per-session 可读运行日志写入器（sessions/<slug>/session.log）。

        slug 在 create_row 后即固定、_session_dir 随时可用，故惰性构造即可，无需在
        __init__ 里持有。写入自身 try/except 降级为 warning，绝不拖垮 session。
        """
        w = getattr(self, "_slog_writer", None)
        if w is None:
            w = SessionLogWriter(self._session_dir() / "session.log")
            self._slog_writer = w
        return w

    def _is_remote(self) -> bool:
        return self.repo_cfg.mode == "remote"

    def _remote_worktree_path(self) -> str:
        """remote 模式远端 worktree 的确定性路径，与 dev_protocol_remote.md 约定一致：
        ``{remote_worktree_root}/{display_slug}``。主控据此拼 SSH diff / 提交校验命令。"""
        slug = self.row.get("display_slug") or self.slug
        return f"{self.repo_cfg.remote_worktree_root}/{slug}"

    def _configured_platform(self) -> str:
        """repo 显式配置的平台；auto 回退 gitlab。

        用于无法本地探测 origin 的场景（如 remote 模式渲染 dev prompt）——此时只能信配置。
        """
        p = getattr(self.repo_cfg, "platform", "auto")
        return p if p in ("gitlab", "github") else "gitlab"

    async def _resolve_platform(self, worktree: Path) -> str:
        """决定提 MR/PR 的目标平台（local 模式主控提交时用）：

        - repo 配置显式指定（gitlab / github）→ 直接用
        - auto + local 模式 → 探测 worktree 的 origin remote URL
        - auto + remote 模式 → 本地无 origin 可探测，回退 gitlab（保持 GitLab-first 默认）
        """
        configured = getattr(self.repo_cfg, "platform", "auto")
        if configured in ("gitlab", "github"):
            return configured
        if self._is_remote():
            return "gitlab"
        return await mr_module.detect_remote_platform(worktree)

    def _claude_extra_env(self, worktree: Path) -> dict[str, str]:
        """统一构造 plan / dev 阶段 claude 子进程的 extra_env。

        - `CC_FLEET_WORKTREE`：本地 cwd，hook 用作白名单主前缀。
        - `CC_FLEET_EXTRA_WORKTREE_ROOTS`（remote 模式注入）：把远端项目根与远端 worktree 根
          以 `os.pathsep` 分隔注入，让 hook 不把 claude 经 `ssh <host> '…'` 操作的远端绝对
          路径误判成"工作目录外的写"。
        """
        env: dict[str, str] = {"CC_FLEET_WORKTREE": str(worktree)}
        if self._is_remote():
            extras = [
                self.repo_cfg.remote_repo_path or "",
                self.repo_cfg.remote_worktree_root or "",
            ]
            joined = os.pathsep.join(p for p in extras if p.strip())
            if joined:
                env["CC_FLEET_EXTRA_WORKTREE_ROOTS"] = joined
        return env

    def _render_dev_system_prompt(self) -> str:
        """按 mode 选 dev_protocol 模板、做占位符替换，落盘后返回内容文本。

        remote 模式（defer-push）的 dev 协议只到 commit、不含发布步骤，故不再拼 forge 片段；
        push + 建 MR/PR 由 publish 阶段的 _render_publish_system_prompt 负责。仍把渲染结果
        落盘到 ``dev_system_prompt.md``（审计 / 排查用），同时返回文本供 runner 的
        protocol_text 直接注入。
        """
        sdir = self._session_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        out = sdir / "dev_system_prompt.md"
        if self._is_remote():
            tpl = _prompt_text("dev_protocol_remote.md").read_text(encoding="utf-8")
            content = self._format_remote_prompt(tpl)
        else:
            content = _prompt_text("dev_protocol_local.md").read_text(encoding="utf-8")
        out.write_text(content, encoding="utf-8")
        return content

    def _render_publish_system_prompt(self) -> str:
        """渲染 remote 发布阶段 system prompt：publish_protocol_remote.md + forge_remote_{platform}。

        与旧 dev-remote 的渲染同构：先把 {forge_workflow} 替换成对应平台的 push+建 MR 片段，
        再统一 format 展开 {display_slug} / {default_branch} 等占位（片段内也含这些占位）。
        仍把渲染结果落盘到 ``publish_system_prompt.md``，同时返回文本供 protocol_text 注入。
        """
        sdir = self._session_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        out = sdir / "publish_system_prompt.md"
        tpl = _prompt_text("publish_protocol_remote.md").read_text(encoding="utf-8")
        forge = _prompt_text(
            f"forge_remote_{self._configured_platform()}.md"
        ).read_text(encoding="utf-8")
        content = self._format_remote_prompt(tpl.replace("{forge_workflow}", forge))
        out.write_text(content, encoding="utf-8")
        return content

    def _format_remote_prompt(self, tpl: str) -> str:
        """统一展开 remote prompt 模板里的占位符（dev / publish 共用）。"""
        return tpl.format(
            remote_ssh_alias=self.repo_cfg.remote_ssh_alias or "",
            remote_repo_path=self.repo_cfg.remote_repo_path or "",
            remote_worktree_root=self.repo_cfg.remote_worktree_root or "",
            default_branch=self.row["default_branch"],
            display_slug=self.row.get("display_slug") or self.slug,
        )

    def _tag(self) -> str:
        """嵌入到外发消息末尾的标识符行。

        - slug 优先用 display_slug（claude 给的可读名），回退 internal slug
        - repo 与 claude_session_id 任一缺位时 format_session_tag 自动省略对应段
        - 带 sid 是为了机器人异常时用户能 `claude --resume <sid>` 手工兜底
        """
        s = self.row.get("display_slug") or self.slug
        return "\n\n" + format_session_tag(
            s,
            repo=self.repo_cfg.name,
            claude_session_id=self.row.get("claude_session_id"),
        )

    async def _mr_title(self) -> str:
        """三级优先：claude 协议输出 → 最近一条 commit subject → initial_request 首行。"""
        if self._pending_mr_title:
            return self._pending_mr_title
        subjects = await self._commit_subjects_ahead()
        if subjects:
            # git log 默认新→旧，subjects[0] 是最新一条；commit subject 本身已是
            # claude 写的中文工作概括，直接用就比 initial_request 首行靠谱。
            return subjects[0][:200]
        request = (self.row.get("initial_request") or "").strip()
        head = request.splitlines()[0][:60] if request else (self.row.get("display_slug") or self.slug)
        return head or "自动开发"

    async def _mr_description(self) -> str:
        """两路：claude 协议输出 → git log 拼装的兜底模板。

        兜底模板把 commit log 落进「改动概要」、initial_request 落进「用户原始需求」，
        其他强制小节占位并标注"未由 claude 提供"，提醒 reviewer 关注。
        """
        if self._pending_mr_description:
            return self._pending_mr_description
        return await self._build_fallback_description()

    async def _commit_subjects_ahead(self) -> list[str]:
        """读取 worktree 相对 origin/<default_branch> 的 commit subject 列表（新→旧）。"""
        wt = self.row.get("worktree_path")
        if not wt:
            return []
        try:
            return await repo_module.get_commits_ahead_subjects(
                Path(wt), f"origin/{self.row['default_branch']}"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("session %s 读取 commit subject 失败：%s", self.slug, e)
            return []

    async def _build_fallback_description(self) -> str:
        """claude 没按协议输出 description 时，主控用 git log + initial_request 兜底拼装。

        小节顺序与 dev_protocol 中规约一致：背景 / 用户原始需求 / 改动概要 / 测试与验证
        / 文档与注释同步 / 风险与回滚。无法填的位置标注"未由 claude 提供"，让 reviewer 知道
        这是兜底产物而不是模型有意省略。
        """
        slug = self.row.get("display_slug") or self.slug
        request = (self.row.get("initial_request") or "").strip()
        request_quote = (
            "\n".join(f"> {line}" if line else ">" for line in request.splitlines())
            if request
            else "> （无）"
        )

        subjects = await self._commit_subjects_ahead()
        changes = (
            "\n".join(f"- {s}" for s in subjects)
            if subjects
            else "- （未由 claude 提供改动概要，建议查看 commit 列表）"
        )

        return (
            "> 由 cc-fleet 主控兜底生成（claude 未按协议输出完整 MR 描述）。\n\n"
            "## 背景\n"
            f"session `{slug}`。\n\n"
            "## 用户原始需求\n"
            f"{request_quote}\n\n"
            "## 改动概要\n"
            f"{changes}\n\n"
            "## 测试与验证\n"
            "- 未由 claude 提供，建议 reviewer 关注。\n\n"
            "## 文档与注释同步\n"
            "- 未由 claude 提供，建议 reviewer 关注。\n\n"
            "## 风险与回滚\n"
            "- 未由 claude 提供，建议 reviewer 关注。"
        )

    def _resume_target_state(self) -> SessionState | None:
        """根据当前 state + failed_phase 决定 follow-up 唤醒后应进入的工作态。

        - COMPLETED → DEVELOPING（在已有 worktree 上把 followup 注入为 dev 追加反馈；
          不重做 plan。plan 阶段是强协议模式 + 禁写文件，与"解决冲突 / 微调"这类
          操作型 followup 语义互斥，强行走 plan 会让 claude 实际干活但解析器报
          STATUS 字段缺失。如需换方向请 @<repo> 开新 session）
        - FAILED / TIMEOUT 按 failed_phase 精细化：
          * planning → PLANNING（复用澄清路径，pending_user_message 注入 plan prompt）
          * plan_reviewing → DEVELOPING（plan 已 READY 落盘，Reviewer 失败本就跳过，直接进开发）
          * developing → DEVELOPING（pending_user_message 注入 dev prompt）
          * code_reviewing → DEVELOPING（代码已 commit，回 dev 让 claude 据 followup 续改）
          * mr_submitting → local 回 DEVELOPING（mr 阶段不调 claude，回 dev 兜底）；
            remote 回 MR_SUBMITTING（remote 发布是 claude 调用，失败直接重试发布即可）
          * new → 返回 None，环境创建失败无法续
          * 其他/NULL（老 row 缺字段）→ 走 last_error 启发兜底，再不行落 PLANNING

        注：plan_reviewing / code_reviewing 阶段 Reviewer 失败一律被吞掉跳过，正常不会以
        这两个 phase 落 FAILED；仅主控在审查中途崩溃等极端情形才会，故映射到 DEVELOPING。
        """
        state = SessionState(self.row["state"])
        if state == SessionState.COMPLETED:
            return SessionState.DEVELOPING

        phase = self.row.get("failed_phase") or self._infer_phase_from_last_error()
        if phase == SessionState.NEW.value:
            return None
        if phase == SessionState.PLANNING.value:
            return SessionState.PLANNING
        # remote 的发布阶段是 claude 调用（push+建 MR），失败直接重试发布；
        # local 的 mr_submitting 不调 claude，仍回 dev 兜底。
        if phase == SessionState.MR_SUBMITTING.value and self._is_remote():
            return SessionState.MR_SUBMITTING
        if phase in (
            SessionState.PLAN_REVIEWING.value,
            SessionState.DEVELOPING.value,
            SessionState.CODE_REVIEWING.value,
            SessionState.MR_SUBMITTING.value,
        ):
            return SessionState.DEVELOPING
        return SessionState.PLANNING  # 兜底（含 awaiting 不会到这里 / 未知 phase）

    def _infer_phase_from_last_error(self) -> str | None:
        """老 row 没 failed_phase 字段时，从 last_error 文本里启发式推断。"""
        msg = (self.row.get("last_error") or "").lower()
        if not msg:
            return None
        if "创建 worktree" in self.row.get("last_error", ""):
            return SessionState.NEW.value
        if "mr" in msg or "push" in msg:
            return SessionState.MR_SUBMITTING.value
        if "dev" in msg or "开发" in self.row.get("last_error", ""):
            return SessionState.DEVELOPING.value
        if "plan" in msg or "澄清" in self.row.get("last_error", ""):
            return SessionState.PLANNING.value
        return None

    def _worktree_intact(self) -> bool:
        """follow-up 唤醒前的环境校验。remote 模式下 worktree 在远端，跳过本地检查。"""
        if self._is_remote():
            return True
        wt = self.row.get("worktree_path")
        if not wt:
            return False
        return Path(wt).is_dir()

    async def _persist_claude_event(self, evt: dict) -> None:
        """把 claude SDK stream-json 的每条事件原文落到 events 表，kind 加 claude. 前缀。

        同一 choke point 旁挂人类可读日志：渲染工具调用输入/返回、模型文本、终态，
        去噪后追加进 sessions/<slug>/session.log（plan/dev/publish 各阶段都经此回调）。
        """
        etype = evt.get("type") or "unknown"
        await self.db.add_event(self.slug, f"claude.{etype}", evt)
        self._session_log().write_event(evt)

    async def _refresh_row(self) -> None:
        row = await self.db.get_session(self.slug)
        if row is None:
            raise RuntimeError(f"session {self.slug} 不存在于 db")
        self.row = row

    async def _update(self, **fields: Any) -> None:
        await self.db.update_session(self.slug, **fields)
        await self._refresh_row()

    async def _set_state(self, state: SessionState, **extra: Any) -> None:
        """写 DB state + 落 event。带 CANCELLED 终态吸收守卫。

        守卫语义：若 DB 当前已是 CANCELLED（``is_open=False``，唯一不可恢复终态），
        则**静默吸收**任何后续状态写入。``/cancel`` 是软取消——不 kill 正在跑的
        claude 子进程；若不设防，``_do_developing`` 等 action 末尾的
        ``_set_state(下一态)``、以及 ``_fail`` / ``_timeout``，会把 ``CANCELLED``
        覆盖回 ``MR_SUBMITTING`` / ``FAILED`` 等，导致 drive loop 误以为还要继续
        推进，从而真的去 push 并建 MR。
        RESUMABLE_TERMINAL（FAILED/TIMEOUT/COMPLETED）可被 ``apply_followup`` 引用
        回复唤醒回 WORKING，是合法转换——这里**不**对它们设防。
        """
        await self._refresh_row()
        current = SessionState(self.row["state"])
        if current == SessionState.CANCELLED and state != SessionState.CANCELLED:
            logger.info(
                "session %s 已 CANCELLED，忽略 _set_state(%s)（/cancel 抢占了状态机推进）",
                self.slug,
                state.value,
            )
            return
        await self._update(state=state.value, **extra)
        await self.db.add_event(self.slug, "state", {"to": state.value, **extra})
        # 可读日志里补一条阶段流转标题——这是 stream.jsonl 结构上没有的主控侧信息。
        self._session_log().write_phase(state.value.upper())

    async def _fail(self, reason: str, phase: str | None = None) -> None:
        # 记录失败时所处阶段，供后续引用回复唤醒决定 resume 回到哪个状态。
        # 默认取当前 state（_set_state 前 self.row["state"] 还是旧值）。
        phase = phase or self.row.get("state")
        await self._set_state(SessionState.FAILED, last_error=reason, failed_phase=phase)
        await self._notify(f"❌ session 失败：{reason}{self._tag()}")

    async def _timeout(self, reason: str) -> None:
        phase = self.row.get("state")
        await self._set_state(SessionState.TIMEOUT, last_error=reason, failed_phase=phase)

    async def _notify(self, text: str, *, force: bool = False) -> None:
        """对用户发出站消息的唯一汇聚点。带 CANCELLED 抑制守卫（与 _set_state 对称）。

        ``/cancel`` 是软取消——不 kill 正在跑的 claude 子进程；子进程跑完后 action
        会继续执行到末尾，在被 ``_set_state`` 守卫吸收的状态写入之前可能仍调用
        ``_notify*``（如"plan 已就绪，开始开发"、"需要进一步确认"、"❌ session 失败"、
        审查 / 发布各阶段进度通知），这些都是误导消息。故 DB 已是 ``CANCELLED`` 时
        静默吸收任何后续通知，唯独放行 ``cancel()`` 自己的回执（``force=True``）。

        守卫先 ``_refresh_row`` 再判：同进程取消时 ``cancel`` 已把 ``self.row`` 刷成
        ``CANCELLED``，本可直读；但**跨进程**取消（``cc-fleet sessions cancel`` 是独立
        进程写 DB，bot 守护进程内存里的 ``self.row`` 仍停在旧态）必须查库才挡得住。
        出站消息低频，多一次 DB 读可忽略，且与 ``_set_state`` 的"先 refresh 再判"对称。
        """
        if not force:
            await self._refresh_row()
            if SessionState(self.row["state"]) == SessionState.CANCELLED:
                logger.info(
                    "session %s 已 CANCELLED，抑制通知：%s", self.slug, text[:60]
                )
                return
        chatid = self.row.get("chatid") or ""
        await self.db.add_message(self.slug, "out", text)
        # 把发给用户的通知（含 ❌ 失败原因、超时、plan 就绪、澄清等）也缝进可读日志——
        # 这正是「为什么被主控判失败」这半信息，stream.jsonl 里没有。
        self._session_log().write_note(text)
        await self.reply(chatid, text)

    async def _notify_clarification(
        self, questions: list[str], plan_path: Path | None = None
    ) -> None:
        bullets = (
            "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            or "1. （未给出具体问题，请重新描述需求）"
        )
        # plan 阶段附 plan 全文路径；dev 阶段（plan_path=None）无此语义，省略该行。
        plan_line = f"plan 全文：{plan_path}\n\n" if plan_path is not None else ""
        text = (
            f"需要进一步确认：\n{bullets}\n\n"
            f"{plan_line}"
            f"请**引用本消息**回复以补充信息。{self._tag()}"
        )
        await self._notify(text)

    async def _notify_plan_ready(self, plan_path: Path) -> None:
        text = (
            f"plan 已就绪，开始开发 ✅\n"
            f"plan 全文：{plan_path}{self._tag()}"
        )
        await self._notify(text)

    def _write_plan_md(self, text_output: str) -> Path:
        """把 plan 正文（剥协议尾）落到 sessions/<slug>/plan.md，返回绝对路径。"""
        body = strip_plan_protocol_tail(text_output)
        path = self._session_dir() / "plan.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path.resolve()
