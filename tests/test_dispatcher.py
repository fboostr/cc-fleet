"""消息分类规则验证。"""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_fleet.bot.message import IncomingMessage
from cc_fleet.config.schema import (
    AppConfig,
    ClaudeConfig,
    LimitsConfig,
    RepoConfig,
    WecomConfig,
)
from cc_fleet.core.dispatcher import DispatchKind, classify


def make_config(tmp_path: Path) -> AppConfig:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        repos=[
            RepoConfig(
                name="feed-web",
                aliases=["feed", "web"],
                path=repo_root,
                default_branch="main",
                keywords=["前端", "列表页"],
            )
        ],
        limits=LimitsConfig(),
    )


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return make_config(tmp_path)


async def _never_open(_: str) -> bool:
    return False


async def _always_open(_: str) -> bool:
    return True


async def test_mention_routes_to_new(cfg: AppConfig):
    msg = IncomingMessage(text="@feed 在 README 末尾加一行", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW
    assert d.repo and d.repo.name == "feed-web"
    assert d.cleaned_text == "在 README 末尾加一行"


async def test_mention_with_full_name(cfg: AppConfig):
    msg = IncomingMessage(text="@feed-web 加点东西", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.repo.name == "feed-web"


async def test_unknown_mention_is_noise(cfg: AppConfig):
    msg = IncomingMessage(text="@unknown 干啥", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NOISE and "unknown" in d.reason


async def test_keyword_fallback(cfg: AppConfig):
    msg = IncomingMessage(text="帮我改一下列表页的标题", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.repo.name == "feed-web"


async def test_no_repo_match_is_noise(cfg: AppConfig):
    msg = IncomingMessage(text="今天天气真好", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NOISE and "@" in d.reason


async def test_empty_text_is_noise(cfg: AppConfig):
    msg = IncomingMessage(text="   ", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NOISE


async def test_quote_active_session_routes_to_continue(cfg: AppConfig):
    msg = IncomingMessage(
        text="我回复一下你的问题",
        quote_text="需要进一步确认：\n1. 用密码还是 OAuth？\n\n[session: add-login]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.CONTINUE and d.session_slug == "add-login"


async def test_quote_inactive_session_falls_through(cfg: AppConfig):
    """引用了一个已经终止的 session，不应路由为 continue，但若 text 有 @repo 仍可开新。"""
    msg = IncomingMessage(
        text="@feed 再发一次", quote_text="[session: dead]", chatid="c", userid="u"
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.repo.name == "feed-web"


async def test_quote_terminal_session_no_repo_no_keyword_is_noise(cfg: AppConfig):
    """quote 引用了已结束 session、tag 里也没 repo、text 也没显式 @ 也没关键词
    → 兜底 NOISE，提示用户用 @<repo> 指明仓库。"""
    msg = IncomingMessage(
        text="再发一次", quote_text="[session: dead]", chatid="c", userid="u"
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NOISE
    assert "@" in d.reason


async def test_quote_terminal_session_with_repo_routes_to_new(cfg: AppConfig):
    """quote 指向一个 ``is_open=False`` 的 session（如 CANCELLED 或 slug 已被清退）但
    tag 里带 repo → 以 quote 里的 repo 当隐式 mention 开新 session，不要求用户再敲 @<repo>。

    注意：FAILED/TIMEOUT/COMPLETED 在改动后 ``is_open=True``，会走 CONTINUE 而非这条；
    详见 ``test_quote_resumable_terminal_session_routes_to_continue``。"""
    msg = IncomingMessage(
        text="补充一下",
        quote_text="[session: dead @feed-web sid: abcd1234-aaaa-bbbb-cccc-ddddeeeeffff]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW
    assert d.repo and d.repo.name == "feed-web"
    assert d.cleaned_text == "补充一下"


async def test_quote_resumable_terminal_session_routes_to_continue(cfg: AppConfig):
    """quote 指向 FAILED/TIMEOUT/COMPLETED session（``is_open_session=True``，由 state.py
    的 RESUMABLE_TERMINAL 语义覆盖）→ CONTINUE，让 SessionManager 走复活流程。

    这条 + state.py 的 ``is_resumable_terminal`` 不变式共同锁住"引用 bot 失败回执也能
    继续推进"的行为，避免回到旧版"已结案 → 直接开新 session 丢上下文"的体验。"""
    msg = IncomingMessage(
        text="重新提交 MR 试试",
        quote_text=(
            "❌ session 失败：dev 阶段完成但未输出 `MR_URL:` 协议行。\n\n"
            "[session: fix-bug @feed-web sid: abcd1234-aaaa-bbbb-cccc-ddddeeeeffff]"
        ),
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.CONTINUE
    assert d.session_slug == "fix-bug"
    assert d.cleaned_text == "重新提交 MR 试试"


async def test_awaiting_session_routes_to_continue(cfg: AppConfig):
    """语义文档化：awaiting_user_clarification 状态属于 open，引用回复应走 CONTINUE
    而非 NOISE。这条 + state.py 里的 is_open 不变式共同锁住 awaiting 不被遗漏。"""
    msg = IncomingMessage(
        text="用密码",
        quote_text="[session: fix-x @feed-web sid: 02158eab-0b3e-4e82-8905-fd96052e7ed2]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.CONTINUE
    assert d.session_slug == "fix-x"


async def test_quote_initial_ack_internal_slug_routes_to_continue(cfg: AppConfig):
    """初始 ack 在 dispatch 同步路径已挂 ``[session: <internal_slug> @<repo>]``。用户引用
    该消息追加文字时，谓词必须把 internal slug 也认作 open，归类为 CONTINUE，让追加
    内容落到同一 session，而不是误判成 NEW 再开一个。"""
    msg = IncomingMessage(
        text="补一句：先做最小可用版本",
        quote_text=(
            "已收到需求，开始分析 @feed-web。当 plan 完成或需要确认时会再通知你。\n\n"
            "[session: req-20260525-114918-fbd4 @feed-web]"
        ),
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.CONTINUE
    assert d.session_slug == "req-20260525-114918-fbd4"
    assert d.cleaned_text == "补一句：先做最小可用版本"


async def test_quote_repo_only_tag_routes_to_new(cfg: AppConfig):
    """**兼容历史**：早期发出的初始 reply 只挂 ``[repo: ...]``-only tag（生成端已切到
    session tag，但解析端仍兼容）。这种引用 + text 没显式 @ → NEW，以 quote 中 repo
    当隐式 mention。"""
    msg = IncomingMessage(
        text="改一下 README",
        quote_text="已收到需求，开始分析 @feed-web。\n\n[repo: feed-web]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW
    assert d.repo and d.repo.name == "feed-web"
    assert d.cleaned_text == "改一下 README"


async def test_quote_repo_only_tag_with_bot_prefix(cfg: AppConfig):
    """企微自动加的 @ChatBot 前缀剥掉后无显式 alias，应仍走 quote 隐式 repo。"""
    msg = IncomingMessage(
        text="@ChatBot 改一下 README",
        quote_text="[repo: feed-web]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW
    assert d.repo and d.repo.name == "feed-web"


async def test_quote_full_tag_active_routes_to_continue(cfg: AppConfig):
    """带 repo + sid 的新格式 tag 在 active session 时仍走 CONTINUE。"""
    msg = IncomingMessage(
        text="用密码",
        quote_text="[session: add-login @feed-web sid: abcd1234-aaaa-bbbb-cccc-ddddeeeeffff]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.CONTINUE
    assert d.session_slug == "add-login"


async def test_explicit_mention_overrides_quote_repo(cfg: AppConfig):
    """text 显式 @<alias> 应覆盖 quote 里的隐式 repo（用户意图优先）。"""
    msg = IncomingMessage(
        text="@feed 加列表页",
        quote_text="[repo: some-other-repo]",  # 故意指向 cfg 里不存在的 repo
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW
    assert d.repo and d.repo.name == "feed-web"
    assert d.cleaned_text == "加列表页"


async def test_mention_with_newline_after_alias(cfg: AppConfig):
    """`@repo\\n内容...` 这种换行风格（用户在企微里敲完 @ 直接换行写需求）必须能识别。"""
    msg = IncomingMessage(
        text="@feed \n部署检查功能，增加一条通路：\n消费 kafka 部署成功消息",
        quote_text="",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.repo.name == "feed-web"
    assert "部署检查功能" in d.cleaned_text


async def test_group_chat_bot_mention_then_repo(cfg: AppConfig):
    """群聊里企微会在前面自动加 `@ChatBot `；用户再写 `@feed 内容` — 两个 @ 都要正确处理。"""
    msg = IncomingMessage(
        text="@ChatBot @feed 加个登录页",
        quote_text="",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.repo.name == "feed-web"
    assert d.cleaned_text == "加个登录页"


async def test_group_chat_bot_mention_only_keyword_fallback(cfg: AppConfig):
    """群聊里只 at 了机器人，没 at repo，但内容含关键词 → 走 keyword 兜底。"""
    msg = IncomingMessage(
        text="@ChatBot 改一下列表页的标题", quote_text="", chatid="c", userid="u"
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.repo.name == "feed-web"


async def test_group_chat_bot_mention_no_keyword_is_noise(cfg: AppConfig):
    """群聊里只 at 了机器人、内容也没关键词 → 提示找不到 @ChatBot。"""
    msg = IncomingMessage(text="@ChatBot 你好", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NOISE and "ChatBot" in d.reason


# ---------- 单需求级 [review] 内联指令 ----------


async def test_review_directive_on_after_mention(cfg: AppConfig):
    """`@repo [review] 需求` → NEW，review_override=True，标记被剥离。"""
    msg = IncomingMessage(text="@feed [review] 加个限流", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.repo.name == "feed-web"
    assert d.review_override is True
    assert d.cleaned_text == "加个限流"


async def test_review_directive_on_explicit(cfg: AppConfig):
    """`[review:on]` 与 `[review]` 等价。"""
    msg = IncomingMessage(text="@feed [review:on] 加个限流", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.review_override is True and d.cleaned_text == "加个限流"


async def test_review_directive_off(cfg: AppConfig):
    """`[review:off]` → review_override=False，标记被剥离。"""
    msg = IncomingMessage(text="@feed [review:off] 修个错别字", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.review_override is False
    assert d.cleaned_text == "修个错别字"


async def test_review_directive_absent_is_none(cfg: AppConfig):
    """没有标记时 review_override 为 None（跟随 repo 配置）。"""
    msg = IncomingMessage(text="@feed 加个限流", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.review_override is None


async def test_review_directive_mid_text_and_whitespace_collapsed(cfg: AppConfig):
    """标记在正文中段也能识别；剥离后多余空白被折叠。"""
    msg = IncomingMessage(
        text="@feed 改一下登录 [review] 再补测试", quote_text="", chatid="c", userid="u"
    )
    d = await classify(msg, cfg, _never_open)
    assert d.review_override is True
    assert d.cleaned_text == "改一下登录 再补测试"


async def test_review_directive_case_insensitive_and_spaces(cfg: AppConfig):
    """大小写不敏感，且方括号内允许空格。"""
    msg = IncomingMessage(
        text="@feed [ Review : OFF ] 修个错别字", quote_text="", chatid="c", userid="u"
    )
    d = await classify(msg, cfg, _never_open)
    assert d.review_override is False and d.cleaned_text == "修个错别字"


async def test_review_directive_multiple_last_wins(cfg: AppConfig):
    """同一条消息多次出现 → 取最后一次（末次命中约定），所有标记都被剥离。"""
    msg = IncomingMessage(
        text="@feed [review:off] 加功能 [review]", quote_text="", chatid="c", userid="u"
    )
    d = await classify(msg, cfg, _never_open)
    assert d.review_override is True
    assert d.cleaned_text == "加功能"


async def test_review_directive_preserves_multiline_body(cfg: AppConfig):
    """带标记的多行需求：剥标记后仍保留换行结构，不被压成一行。"""
    msg = IncomingMessage(
        text="@feed [review] 实现登录\n\n要求：\n- 限流\n- 审计",
        quote_text="",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.review_override is True
    assert d.cleaned_text == "实现登录\n\n要求：\n- 限流\n- 审计"


async def test_review_directive_on_keyword_path(cfg: AppConfig):
    """keyword 兜底路由的 NEW 也解析标记。"""
    msg = IncomingMessage(
        text="改一下列表页的标题 [review]", quote_text="", chatid="c", userid="u"
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.repo.name == "feed-web"
    assert d.review_override is True
    assert d.cleaned_text == "改一下列表页的标题"


async def test_review_directive_on_implicit_quote_repo_path(cfg: AppConfig):
    """quote 隐式 repo 路由的 NEW 也解析标记。"""
    msg = IncomingMessage(
        text="补充一下 [review]",
        quote_text="[session: dead @feed-web sid: abcd1234-aaaa-bbbb-cccc-ddddeeeeffff]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NEW and d.repo.name == "feed-web"
    assert d.review_override is True and d.cleaned_text == "补充一下"


async def test_review_directive_not_parsed_on_continue(cfg: AppConfig):
    """标记只对新需求生效：CONTINUE（引用活跃 session）不解析、不剥离。"""
    msg = IncomingMessage(
        text="继续改 [review]",
        quote_text="[session: add-login]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.CONTINUE
    assert d.review_override is None
    assert d.cleaned_text == "继续改 [review]"


# ---------- 控制面指令 ----------


async def test_list_command_routes_to_list(cfg: AppConfig):
    msg = IncomingMessage(text="/list", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "list"
    assert d.command_arg is None


async def test_list_command_case_insensitive(cfg: AppConfig):
    msg = IncomingMessage(text="/LIST", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND and d.command == "list"


async def test_list_all_arg(cfg: AppConfig):
    """/list all 应解析为 command="list"，command_arg="all"。"""
    msg = IncomingMessage(text="/list all", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "list"
    assert d.command_arg == "all"


async def test_list_all_arg_case_insensitive(cfg: AppConfig):
    """命令头大小写不敏感；arg 由 commands 层再统一 lower。"""
    msg = IncomingMessage(text="/LIST ALL", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "list"
    assert d.command_arg == "ALL"


async def test_list_command_ignores_quote(cfg: AppConfig):
    """即使引用了某个 session 的消息，/list 仍应路由到 COMMAND 而非 CONTINUE。"""
    msg = IncomingMessage(
        text="/list",
        quote_text="[session: add-login]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.COMMAND and d.command == "list"


async def test_status_command_is_unknown_now(cfg: AppConfig):
    """/status 已硬切移除：应当走 NOISE 的"未知指令"提示，而不是被识别为 list。"""
    msg = IncomingMessage(text="/status", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NOISE
    assert "/status" in d.reason
    # 兜底提示应当指向新的 /list
    assert "/list" in d.reason


async def test_unknown_slash_command_is_noise(cfg: AppConfig):
    msg = IncomingMessage(text="/whatever foo", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.NOISE and "whatever" in d.reason
    # 兜底提示不应再提到 /status
    assert "/status" not in d.reason


async def test_help_command(cfg: AppConfig):
    msg = IncomingMessage(text="/help", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND and d.command == "help"


async def test_repos_command(cfg: AppConfig):
    msg = IncomingMessage(text="/repos", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "repos"
    assert d.command_arg is None


async def test_repos_command_case_insensitive(cfg: AppConfig):
    msg = IncomingMessage(text="/REPOS", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND and d.command == "repos"


async def test_repos_command_ignores_quote(cfg: AppConfig):
    """即使引用某个活跃 session 的消息，/repos 仍应路由到 COMMAND 而非 CONTINUE。"""
    msg = IncomingMessage(
        text="/repos",
        quote_text="[session: add-login]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.COMMAND and d.command == "repos"


async def test_cancel_with_explicit_slug(cfg: AppConfig):
    msg = IncomingMessage(text="/cancel add-login", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "cancel"
    assert d.command_arg == "add-login"


async def test_cancel_no_arg_falls_back_to_quote_slug(cfg: AppConfig):
    """没参数时回退到 quote 里的 slug —— 用户引用某 session 消息发 /cancel 的语义。"""
    msg = IncomingMessage(
        text="/cancel",
        quote_text="[session: add-login @feed-web sid: abcd1234-aaaa-bbbb-cccc-ddddeeeeffff]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "cancel"
    assert d.command_arg == "add-login"


async def test_cancel_no_arg_no_quote_yields_empty_arg(cfg: AppConfig):
    """没参数也没 quote slug → command_arg=None，由 commands 层提示用法。"""
    msg = IncomingMessage(text="/cancel", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "cancel"
    assert d.command_arg is None


async def test_resume_with_explicit_slug(cfg: AppConfig):
    msg = IncomingMessage(text="/resume add-login", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "resume"
    assert d.command_arg == "add-login"


async def test_resume_no_arg_falls_back_to_quote_slug(cfg: AppConfig):
    """没参数时回退到 quote 里的 slug,与 /cancel / /plan 一致,支持"引用某 session 发 /resume"。"""
    msg = IncomingMessage(
        text="/resume",
        quote_text="[session: add-login @feed-web sid: abcd1234-aaaa-bbbb-cccc-ddddeeeeffff]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "resume"
    assert d.command_arg == "add-login"


async def test_plan_with_explicit_slug(cfg: AppConfig):
    msg = IncomingMessage(text="/plan add-login", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "plan"
    assert d.command_arg == "add-login"


async def test_plan_command_case_insensitive(cfg: AppConfig):
    msg = IncomingMessage(text="/PLAN add-login", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND and d.command == "plan"


async def test_plan_no_arg_falls_back_to_quote_slug(cfg: AppConfig):
    """无参时回退到 quote 里的 slug —— 引用某 session 消息发 /plan 的语义。"""
    msg = IncomingMessage(
        text="/plan",
        quote_text="[session: add-login @feed-web sid: abcd1234-aaaa-bbbb-cccc-ddddeeeeffff]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "plan"
    assert d.command_arg == "add-login"


async def test_plan_no_arg_no_quote_yields_empty_arg(cfg: AppConfig):
    msg = IncomingMessage(text="/plan", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "plan"
    assert d.command_arg is None


async def test_plan_command_ignores_quote_continue_route(cfg: AppConfig):
    """即使引用了某个活跃 session 的消息，/plan 仍应路由到 COMMAND 而非 CONTINUE。"""
    msg = IncomingMessage(
        text="/plan",
        quote_text="[session: add-login]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.COMMAND and d.command == "plan"
    # quote 里有 slug 时无参回退
    assert d.command_arg == "add-login"


async def test_plan_explicit_slug_with_selector(cfg: AppConfig):
    """`/plan <slug> review` 显式带选择器时 command_arg 原样透传给 commands 层解析。"""
    msg = IncomingMessage(
        text="/plan add-login review", quote_text="", chatid="c", userid="u"
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "plan"
    assert d.command_arg == "add-login review"


async def test_plan_quote_with_selector_only(cfg: AppConfig):
    """引用某 session 消息发 `/plan review`：quote 提供 slug，选择器拼到 slug 之后。"""
    msg = IncomingMessage(
        text="/plan review",
        quote_text="[session: add-login @feed-web sid: abcd1234-aaaa-bbbb-cccc-ddddeeeeffff]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "plan"
    assert d.command_arg == "add-login review"


async def test_plan_quote_with_selector_code(cfg: AppConfig):
    """引用 + `/plan code` 同理拼成 `<slug> code`。"""
    msg = IncomingMessage(
        text="/plan code",
        quote_text="[session: add-login]",
        chatid="c",
        userid="u",
    )
    d = await classify(msg, cfg, _always_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command_arg == "add-login code"


async def test_plan_selector_only_no_quote_passes_through(cfg: AppConfig):
    """`/plan review` 没有引用时不拼 slug，原样透传，由 commands 层回用法提示。"""
    msg = IncomingMessage(text="/plan review", quote_text="", chatid="c", userid="u")
    d = await classify(msg, cfg, _never_open)
    assert d.kind == DispatchKind.COMMAND
    assert d.command == "plan"
    assert d.command_arg == "review"
