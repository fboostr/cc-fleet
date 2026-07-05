"""App._on_message 的 ack 文案分支测试。

聚焦 NEW 路径的"已加入队列 vs 开始分析"判定：早期串行模型曾用 ``ahead > 0`` 判排队，
在 ``max_concurrent_sessions > 1`` 的并发模式下会把任何 in-flight 都误判成排队。
正确判定应当与 ``_session_loop`` 实际使用的 semaphore 上限对齐。
"""

from __future__ import annotations

from pathlib import Path

from cc_fleet.app import App
from cc_fleet.bot.base import BotRunner
from cc_fleet.bot.message import IncomingMessage
from cc_fleet.config.schema import (
    AppConfig,
    ClaudeConfig,
    LimitsConfig,
    PipelineConfig,
    RepoConfig,
    WecomConfig,
)


def _make_config(
    tmp_path: Path, *, max_concurrent: int, default_mode: str = "dev", multi: bool = False
) -> AppConfig:
    """本文件聚焦 NEW（dev）路径的 ack 分支，故 default_mode 默认 "dev"，让 `@repo 需求`
    直达开发。验证"裸 /chat 无仓库回退"需要 sole_repo 不成立，用 multi=True 造多仓库。"""
    repo = tmp_path / "my-project"
    repo.mkdir(exist_ok=True)
    repos = [
        RepoConfig(
            name="my-project",
            aliases=["myproj"],
            path=repo,
            default_branch="main",
            keywords=["my-project"],
        ),
    ]
    if multi:
        repo2 = tmp_path / "other-project"
        repo2.mkdir(exist_ok=True)
        repos.append(
            RepoConfig(
                name="other-project",
                aliases=["other"],
                path=repo2,
                default_branch="main",
                keywords=["other-kw"],
            )
        )
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        pipeline=PipelineConfig(plan_timeout_sec=10, dev_timeout_sec=10, max_clarify_rounds=2),
        repos=repos,
        default_mode=default_mode,
        limits=LimitsConfig(max_concurrent_sessions=max_concurrent),
    )


class _FakeBot(BotRunner):
    def __init__(self) -> None:
        super().__init__(on_message=lambda _: None)
        self.replies: list[tuple[str, str]] = []

    async def reply(self, chatid: str, text: str) -> None:
        self.replies.append((chatid, text))

    async def run_forever(self) -> None:
        pass


class _FakeManager:
    """最小 SessionManager 替身：实现 new_session（NEW 分支）与 new_chat_session（CHAT 分支）。"""

    def __init__(self, *, ahead: int, chat_note: str | None = None) -> None:
        self._ahead = ahead
        self._chat_note = chat_note
        self.calls: list[tuple[str, str, str, str]] = []
        self.chat_calls: list[tuple[str | None, str, str, str]] = []

    async def new_session(self, *, repo_cfg, text, chatid, userid, review_override=None):
        self.calls.append((repo_cfg.name, text, chatid, userid))
        self.last_review_override = review_override
        return "internal-slug", self._ahead

    async def new_chat_session(self, *, repo_cfg, text, chatid, userid):
        self.chat_calls.append(
            (repo_cfg.name if repo_cfg else None, text, chatid, userid)
        )
        return "chat-xy", self._chat_note


def _build_app(
    tmp_path: Path,
    *,
    max_concurrent: int,
    ahead: int,
    chat_note: str | None = None,
    multi: bool = False,
):
    app = App(_make_config(tmp_path, max_concurrent=max_concurrent, multi=multi))
    bot = _FakeBot()
    mgr = _FakeManager(ahead=ahead, chat_note=chat_note)
    app._bot = bot
    app._manager = mgr  # type: ignore[assignment]
    return app, bot, mgr


async def test_new_session_ack_idle(tmp_path):
    """ahead==0：ack 直接"开始分析"，不带队列字样。

    ack 末尾应携带 ``[session: <internal_slug> @my-project]``：display_slug 要等 plan
    才有，但 internal slug 在 ``new_session`` 同步路径已经分配，挂上后用户立即可以
    引用回复触发 /cancel 或追加文字续推同一 session。
    """
    app, bot, _ = _build_app(tmp_path, max_concurrent=4, ahead=0)
    await app._on_message(IncomingMessage(
        text="@my-project 加登录页", quote_text="", chatid="c1", userid="u1",
    ))
    assert len(bot.replies) == 1
    chatid, ack = bot.replies[0]
    assert chatid == "c1"
    assert "已收到需求，开始分析 @my-project" in ack
    assert "[session: internal-slug @my-project]" in ack
    assert "[repo:" not in ack
    assert "已加入" not in ack


async def test_new_session_ack_below_concurrent_limit_does_not_say_queued(tmp_path):
    """**回归保护**：max_concurrent=4、ahead=1（前面有 1 个 in-flight 但未触达上限），
    后台 task 会立刻 acquire 到槽位开跑，ack 不应回"已加入队列"。

    bug：早期串行模型下 ``ahead > 0`` 等价于"被挡住"；改造为并发后若仍沿用该判断，
    会让用户看到"已加入队列（前面 1 个）"但后台已经在分析。"""
    app, bot, _ = _build_app(tmp_path, max_concurrent=4, ahead=1)
    await app._on_message(IncomingMessage(
        text="@my-project 改个 bug", quote_text="", chatid="c1", userid="u1",
    ))
    assert len(bot.replies) == 1
    _, ack = bot.replies[0]
    assert "已收到需求，开始分析 @my-project" in ack
    assert "[session: internal-slug @my-project]" in ack
    assert "已加入" not in ack
    assert "前面" not in ack


async def test_new_session_ack_at_concurrent_limit_says_queued(tmp_path):
    """max_concurrent=4、ahead=4：槽位已满，新 task 必然排队，ack 应回"已加入队列"。"""
    app, bot, _ = _build_app(tmp_path, max_concurrent=4, ahead=4)
    await app._on_message(IncomingMessage(
        text="@my-project 占满槽位再来一条", quote_text="", chatid="c1", userid="u1",
    ))
    assert len(bot.replies) == 1
    _, ack = bot.replies[0]
    assert "已加入 @my-project 队列（前面 4 个）" in ack
    assert "开始分析时会再通知你" in ack
    assert "[session: internal-slug @my-project]" in ack


async def test_new_session_ack_single_concurrent_still_queues(tmp_path):
    """max_concurrent=1、ahead=1：单串行模式下任一 in-flight 都会让新 task 排队，
    ack 应保留排队文案——保护原有行为不被本次修复误伤。"""
    app, bot, _ = _build_app(tmp_path, max_concurrent=1, ahead=1)
    await app._on_message(IncomingMessage(
        text="@my-project 来一条", quote_text="", chatid="c1", userid="u1",
    ))
    assert len(bot.replies) == 1
    _, ack = bot.replies[0]
    assert "已加入 @my-project 队列（前面 1 个）" in ack
    assert "[session: internal-slug @my-project]" in ack


async def test_new_session_review_directive_passes_override_and_acks(tmp_path):
    """需求带 [review] → review_override=True 透传给 new_session，ack 提示已开启。"""
    app, bot, mgr = _build_app(tmp_path, max_concurrent=4, ahead=0)
    await app._on_message(IncomingMessage(
        text="@my-project [review] 加登录页", quote_text="", chatid="c1", userid="u1",
    ))
    # 标记已被剥离，初始需求文本不含 [review]
    assert mgr.calls[0][1] == "加登录页"
    assert mgr.last_review_override is True
    _, ack = bot.replies[0]
    assert "已为本需求开启 Reviewer 审查" in ack


async def test_new_session_review_off_directive_passes_override_and_acks(tmp_path):
    """需求带 [review:off] → review_override=False 透传，ack 提示已关闭。"""
    app, bot, mgr = _build_app(tmp_path, max_concurrent=4, ahead=0)
    await app._on_message(IncomingMessage(
        text="@my-project [review:off] 修错别字", quote_text="", chatid="c1", userid="u1",
    ))
    assert mgr.calls[0][1] == "修错别字"
    assert mgr.last_review_override is False
    _, ack = bot.replies[0]
    assert "已为本需求关闭 Reviewer 审查" in ack


async def test_new_session_no_directive_override_none(tmp_path):
    """无标记 → review_override=None，ack 不含 Reviewer 提示。"""
    app, bot, mgr = _build_app(tmp_path, max_concurrent=4, ahead=0)
    await app._on_message(IncomingMessage(
        text="@my-project 加登录页", quote_text="", chatid="c1", userid="u1",
    ))
    assert mgr.last_review_override is None
    _, ack = bot.replies[0]
    assert "Reviewer 审查" not in ack


# ---------- /chat 分支 ----------


async def test_chat_command_binds_repo_and_acks_with_tag(tmp_path):
    """`@repo /chat <msg>` → 调 new_chat_session（绑定 repo），ack 带可引用的 tag、无回退警告。"""
    app, bot, mgr = _build_app(tmp_path, max_concurrent=4, ahead=0)
    await app._on_message(IncomingMessage(
        text="@my-project /chat 看看入口在哪", quote_text="", chatid="c1", userid="u1",
    ))
    assert mgr.chat_calls == [("my-project", "看看入口在哪", "c1", "u1")]
    chatid, ack = bot.replies[0]
    assert chatid == "c1"
    assert "已开始对话 [chat-xy]" in ack
    assert "[session: chat-xy @my-project]" in ack


async def test_chat_command_no_repo_includes_fallback_note(tmp_path):
    """裸 `/chat <msg>`（多仓库、无 @repo）→ ack 包含回退警告；tag 不带 @repo。

    需多仓库：单仓库时裸 /chat 会自动绑定唯一仓库，不再走无仓库回退。"""
    app, bot, mgr = _build_app(
        tmp_path, max_concurrent=4, ahead=0, chat_note="⚠️ 未指定 @repo，回退目录 X", multi=True
    )
    await app._on_message(IncomingMessage(
        text="/chat 你好", quote_text="", chatid="c1", userid="u1",
    ))
    assert mgr.chat_calls == [(None, "你好", "c1", "u1")]
    _, ack = bot.replies[0]
    assert "⚠️ 未指定 @repo，回退目录 X" in ack
    assert "[session: chat-xy]" in ack
    assert "@my-project" not in ack


async def test_chat_command_empty_message_usage_hint(tmp_path):
    """空 `/chat`（无正文）→ 回用法提示，不建会话。"""
    app, bot, mgr = _build_app(tmp_path, max_concurrent=4, ahead=0)
    await app._on_message(IncomingMessage(
        text="/chat", quote_text="", chatid="c1", userid="u1",
    ))
    assert mgr.chat_calls == []
    _, ack = bot.replies[0]
    assert "用法" in ack and "/chat" in ack
