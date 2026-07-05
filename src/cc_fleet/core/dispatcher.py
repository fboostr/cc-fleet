"""消息分类：把进来的消息归到 command / new / continue / chat / handoff / noise 六类。

无显式命令的普通消息，最终走 chat 还是 dev（NEW）由 ``config.default_mode`` 决定
（默认 chat：先对话讨论，聊清后引用消息发 /dev 转开发；dev：一句话直达交付流水线）。
下方规则 2/3/4/5 里凡"按 default_mode 归类"的分支都经 ``_default_entry``。

规则按顺序：
0. text 以 `/` 起头 → 控制面指令（command）。优先于 quote 判定。
   其中 `/chat` → CHAT（自由对话，单仓库自动绑定唯一仓库）；`/dev` → 引用 chat 消息时
   HANDOFF（把讨论转开发），不带引用时 `/dev <需求>` 直达开发（NEW，单仓库自动定位）。
1. quote_text 中能解析出 [session: <slug> ...] 且 slug 在 storage 中且 ``is_open``
   （含 awaiting 与可恢复终态 FAILED/TIMEOUT/COMPLETED）→ CONTINUE。
2. text 以已知 `@<alias>` 起头（可能要先剥掉企微自动加的 `@<bot>`）→ 按 default_mode 归类；
   `@repo /chat` → CHAT、`@repo /dev <需求>` → NEW（直达开发）。显式 @ 始终优先于 quote。
3. text 没显式 @，但 quote 中能解析出 repo（完整 session tag 或 `[repo: ...]` repo-only tag）
   → 以 quote 中的 repo 当隐式 mention，按 default_mode 归类。
   适用于：quote 指向 CANCELLED 的 session、quote 里 slug 不存在于 storage、
   或 quote 只携带 repo-only tag。引用 CANCELLED 视为"放弃后想发新需求"。
4. text 中提到了某个 repo 的 keyword → 推断 repo，按 default_mode 归类。
5. 单仓库兜底：只配了一个仓库时，无从定位的消息一律归属它（免 @），按 default_mode 归类。
6. 兜底：NOISE（机器人回一句提示，不开新 session）。仅在配置了多个仓库且无法定位时触达。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from ..bot.message import IncomingMessage
from ..config.schema import AppConfig, RepoConfig
from ..util.ids import extract_quote_context
from .commands import PLAN_SELECTOR_ALIASES


class DispatchKind(str, Enum):
    NEW = "new"
    CONTINUE = "continue"
    COMMAND = "command"
    CHAT = "chat"
    HANDOFF = "handoff"
    NOISE = "noise"


# 控制面指令映射。/status 已废弃（硬切），改用 /list；默认只显示最近 7 天工作中/等待回复，
# `/list all` 显示最近 7 天所有状态，超过 7 天到网页端查看（语义见 commands.render_list）。
_COMMAND_ALIASES: dict[str, str] = {
    "/list": "list",
    "/help": "help",
    "/cancel": "cancel",
    "/resume": "resume",
    "/plan": "plan",
    "/repos": "repos",
}


@dataclass
class DispatchDecision:
    kind: DispatchKind
    repo: RepoConfig | None = None
    session_slug: str | None = None
    cleaned_text: str = ""               # 移除 @repo 前缀后的文本
    reason: str = ""                     # noise / 拒绝时的中文提示
    command: str | None = None           # COMMAND 时的归一化命令名
    command_arg: str | None = None       # COMMAND 时的剩余参数文本（可空）
    # NEW 时从需求文本解析出的单需求级 review 覆盖：None=不覆盖（跟随 repo 配置），
    # True=本次强制开启 Reviewer，False=本次强制关闭。来源 [review]/[review:off] 内联指令。
    review_override: bool | None = None


# 匹配开头的 `@<alias>` 前缀；alias 允许字母数字下划线短横线、点（域名风格）
# DOTALL 让 `(.+)` 能跨换行匹配后续多行内容
_MENTION_PATTERN = re.compile(r"^\s*@([\w.-]+)\s+(.+)\s*$", re.DOTALL)

# 单需求级 review 内联指令：`[review]` / `[review:on]` 本次强制开；`[review:off]` 本次强制关。
# 方括号包裹 + 大小写不敏感，避免与自然语言里裸写的 "review" 误撞。
_REVIEW_DIRECTIVE = re.compile(r"\[\s*review(?:\s*:\s*(on|off))?\s*\]", re.IGNORECASE)


def _extract_review_directive(text: str) -> tuple[bool | None, str]:
    """从需求文本里抽取并剥离 review 指令。

    返回 ``(override, cleaned)``：override 为 None（无指令）/ True（开）/ False（关）；
    cleaned 是剥掉全部指令并折叠多余空白后的需求正文。多次出现时取**最后一次**
    （与项目其它协议解析"末次命中"约定一致）。
    """
    matches = list(_REVIEW_DIRECTIVE.finditer(text))
    if not matches:
        return None, text
    val = (matches[-1].group(1) or "on").lower()
    # 只折叠剥离标记后多出来的**水平**空白（空格/制表符），保留换行，避免把多行需求拍平成一行。
    cleaned = re.sub(r"[ \t]{2,}", " ", _REVIEW_DIRECTIVE.sub("", text)).strip()
    return (val != "off"), cleaned


def _new_decision(repo: RepoConfig, text: str) -> DispatchDecision:
    """构造 NEW 决策：顺带解析并剥离需求文本里的 [review] 内联指令。

    标记只在 NEW 路径解析；剥离后的纯净文本才进 ``initial_request``，不污染发给 claude 的 prompt。
    """
    override, cleaned = _extract_review_directive(text)
    return DispatchDecision(
        kind=DispatchKind.NEW,
        repo=repo,
        cleaned_text=cleaned,
        review_override=override,
    )


# `/chat` 指令头（大小写不敏感）：识别裸 `/chat <msg>` 与 `@repo /chat <msg>`。
_CHAT_COMMAND = "/chat"

# `/dev` 指令头（大小写不敏感）：引用一条 /chat 对话消息，把讨论转成正式开发任务（handoff）。
_HANDOFF_COMMAND = "/dev"


def _extract_chat_command(text: str) -> str | None:
    """若 text 以 `/chat`（后接空格或结尾）开头，返回其后的消息正文（可空串）；否则 None。

    `/chatxxx` 这类非精确前缀不误判（partition 出的 head 必须恰好等于 `/chat`）。
    """
    head, _, rest = text.partition(" ")
    if head.lower() == _CHAT_COMMAND:
        return rest.strip()
    return None


def _extract_dev_command(text: str) -> str | None:
    """若 text 以 `/dev`（后接空格或结尾）开头，返回其后的正文（可空串）；否则 None。

    用于识别 `@repo /dev <需求>` —— 剥出 repo 后跳过闲聊直达开发。与 `/chatxxx` 同理，
    `/devxxx` 这类非精确前缀不误判。
    """
    head, _, rest = text.partition(" ")
    if head.lower() == _HANDOFF_COMMAND:
        return rest.strip()
    return None


def _sole_repo(config: AppConfig) -> RepoConfig | None:
    """只配置了一个仓库时返回它，否则 None。

    单仓库场景下，无从 @/keyword/quote 定位的消息一律归属这唯一仓库（免 @），贴合普通用户
    "直接说话"的习惯；多仓库时返回 None，保持"必须 @<repo> 指明仓库"的旧行为。
    """
    return config.repos[0] if len(config.repos) == 1 else None


def _default_entry(repo: RepoConfig, text: str, config: AppConfig) -> DispatchDecision:
    """无显式命令的普通消息，按 ``config.default_mode`` 决定进对话还是开发。

    - default_mode='chat'（默认）：CHAT 多轮讨论。``[review]`` 内联指令对 chat 无意义，
      不解析、不剥离（保持对话正文原样）。
    - default_mode='dev'：走 NEW 交付流水线（复用 ``_new_decision``，解析并剥离 ``[review]``）。
    """
    if config.default_mode == "chat":
        return DispatchDecision(
            kind=DispatchKind.CHAT,
            repo=repo,
            cleaned_text=text.strip(),
        )
    return _new_decision(repo, text)


def _match_by_keyword(text: str, repos: list[RepoConfig]) -> RepoConfig | None:
    for repo in repos:
        for kw in repo.keywords:
            if kw and kw in text:
                return repo
    return None


async def classify(
    msg: IncomingMessage,
    config: AppConfig,
    is_open_session: Callable[[str], Awaitable[bool]],
    session_kind_of: Callable[[str], Awaitable[str | None]] | None = None,
) -> DispatchDecision:
    """对一条消息进行分类。

    `is_open_session(slug)`：异步谓词，判断 slug 在 storage 中且 ``is_open``
    （含 awaiting 与可恢复终态 FAILED/TIMEOUT/COMPLETED）。

    `session_kind_of(slug)`：异步谓词，返回 slug 对应 session 的 ``session_kind``
    （'pipeline'/'chat'），不存在返回 None。仅 `/dev`（handoff）用来校验被引用的是
    chat 会话——它需要在 chat 处于非 open（如已 /cancel）时仍能转、且要在引用指向
    pipeline 时拒绝，这是 `is_open_session` 表达不了的。None（未注入）时跳过该校验。
    """
    text = (msg.text or "").strip()
    quote = msg.quote_text or ""

    if not text:
        return DispatchDecision(
            kind=DispatchKind.NOISE,
            reason="请用文字描述需求；当前消息为空或仅含非文本内容。",
        )

    # 0. 控制面指令：/xxx 开头，比 quote/路由判断都优先
    if text.startswith("/"):
        head, _, rest = text.partition(" ")
        if head == text:
            head, rest = text, ""
        # /chat：进入自由对话通道。单仓库时自动绑定唯一仓库（免 @）；多仓库未带 @repo 时
        # repo=None，由 SessionManager 解析回退目录并警告。
        if head.lower() == _CHAT_COMMAND:
            return DispatchDecision(
                kind=DispatchKind.CHAT,
                repo=_sole_repo(config),
                cleaned_text=rest.strip(),
            )
        # /dev：两种用法。
        #  (a) 引用一条 /chat 对话消息 → handoff：把讨论转成正式开发任务（复用对话上下文）。
        #  (b) 不带引用 `/dev <需求>` → 直达开发：老手跳过闲聊，单仓库自动定位仓库。
        if head.lower() == _HANDOFF_COMMAND:
            supplement = rest.strip()
            hctx = extract_quote_context(quote)
            if hctx.slug:
                # (a) handoff：quote 里有 slug，必须是一条 chat 会话；否则回 NOISE 引导。
                if session_kind_of is not None:
                    kind = await session_kind_of(hctx.slug)
                    if kind is None:
                        return DispatchDecision(
                            kind=DispatchKind.NOISE,
                            reason=f"未找到被引用的会话 [{hctx.slug}]。"
                            "请引用最近的 /chat 对话消息再发 /dev。",
                        )
                    if kind != "chat":
                        return DispatchDecision(
                            kind=DispatchKind.NOISE,
                            reason="/dev 只能引用 /chat 对话消息把讨论转成开发任务；"
                            "这条引用不是 chat 会话。若要发新需求请用 `@<repo> 需求`。",
                        )
                # 复用 NEW 路径的 [review] 内联指令解析：补充说明里可带 [review]/[review:off]。
                override, cleaned = (
                    _extract_review_directive(supplement) if supplement else (None, "")
                )
                return DispatchDecision(
                    kind=DispatchKind.HANDOFF,
                    session_slug=hctx.slug,
                    cleaned_text=cleaned,
                    review_override=override,
                )
            # (b) 无引用直达开发：`/dev <需求>`。单仓库自动定位；多仓库无法定位则引导。
            sole = _sole_repo(config)
            if supplement and sole is not None:
                return _new_decision(sole, supplement)
            if not supplement:
                return DispatchDecision(
                    kind=DispatchKind.NOISE,
                    reason="/dev 有两种用法：引用一条 /chat 对话消息把讨论转成开发任务，"
                    "或 `/dev <需求>` 直接开始开发（多仓库请用 `@<repo> /dev <需求>` 指明仓库）。",
                )
            return DispatchDecision(
                kind=DispatchKind.NOISE,
                reason="配置了多个仓库，无法确定 `/dev <需求>` 属于哪个。"
                "请用 `@<repo> /dev <需求>` 指明仓库，或引用某条 /chat 对话消息再发 /dev。",
            )
        normalized = _COMMAND_ALIASES.get(head.lower())
        if normalized is not None:
            arg = rest.strip() or None
            # /cancel / /plan / /resume 无参时回退到 quote 里的 slug,方便用户"引用某
            # session 消息发 /xxx"。
            if normalized in ("cancel", "plan", "resume") and arg is None:
                quote_ctx = extract_quote_context(quote)
                if quote_ctx.slug:
                    arg = quote_ctx.slug
            # /plan 「引用 + 仅选择器」（如引用消息发 `/plan review`）：quote 提供 slug,
            # 用户输入当文档选择器,拼成 `<slug> <selector>` 交给 render_plan 解析。
            elif normalized == "plan" and arg is not None and arg.lower() in PLAN_SELECTOR_ALIASES:
                quote_ctx = extract_quote_context(quote)
                if quote_ctx.slug:
                    arg = f"{quote_ctx.slug} {arg}"
            return DispatchDecision(
                kind=DispatchKind.COMMAND,
                command=normalized,
                command_arg=arg,
                cleaned_text=text,
            )
        return DispatchDecision(
            kind=DispatchKind.NOISE,
            reason=f"未知指令 {head}。可用指令：/list 查看最近 7 天 session（加 `all` 看全部状态）；/help 查看帮助。",
        )

    # 1. 引用消息解析（slug / repo / sid 都可空）
    ctx = extract_quote_context(quote)
    if ctx.slug and await is_open_session(ctx.slug):
        return DispatchDecision(
            kind=DispatchKind.CONTINUE,
            session_slug=ctx.slug,
            cleaned_text=text,
        )

    # 2. 循环剥掉开头的 `@xxx` 前缀；命中第一个已知 repo alias 即采纳。
    #    群聊里企微会在消息前自动加 `@<机器人名>`，再加用户输入的 `@<repo>`，
    #    此时开头会有多个 `@`，必须逐个识别才能正确路由。
    cursor = text
    last_unknown_alias: str | None = None
    while True:
        m = _MENTION_PATTERN.match(cursor)
        if not m:
            break
        alias, rest = m.group(1), m.group(2).strip()
        repo = config.repo_by_name_or_alias(alias)
        if repo is not None:
            # `@repo /chat <msg>` → 绑定该 repo 的自由对话。
            chat_msg = _extract_chat_command(rest)
            if chat_msg is not None:
                return DispatchDecision(
                    kind=DispatchKind.CHAT,
                    repo=repo,
                    cleaned_text=chat_msg,
                )
            # `@repo /dev <需求>` → 跳过闲聊直达开发（NEW），不受 default_mode 影响。
            dev_msg = _extract_dev_command(rest)
            if dev_msg is not None:
                return _new_decision(repo, dev_msg)
            # 显式 @ 优先：忽略 quote 里的隐式信息。普通消息按 default_mode 决定 chat / dev。
            return _default_entry(repo, rest, config)
        last_unknown_alias = alias
        cursor = rest

    # 3. text 无显式 @、quote 里能解析出 repo（slug 可能不存在、可能指向 CANCELLED）
    #    → 以 quote 中的 repo 作为隐式 mention；按 default_mode 决定进对话还是开发。
    #    FAILED/TIMEOUT/COMPLETED 走规则 1 CONTINUE，不会落到这里
    if ctx.repo:
        implicit_repo = config.repo_by_name_or_alias(ctx.repo)
        if implicit_repo is not None:
            return _default_entry(implicit_repo, cursor or text, config)

    # 4. 关键词兜底（用剥过 @ 之后的 cursor）
    fallback_text = cursor or text
    repo = _match_by_keyword(fallback_text, config.repos)
    if repo is not None:
        return _default_entry(repo, fallback_text, config)

    # 5. 单仓库兜底：只配了一个仓库时，无从 @/keyword/quote 定位的消息一律归属它（免 @）。
    #    覆盖个人微信 1:1"直接说话"、以及群聊只 @bot 未 @repo 的场景；含未知 @alias 也归属
    #    这唯一仓库（反正只有一个目标）。按 default_mode 决定进对话还是开发。
    sole = _sole_repo(config)
    if sole is not None:
        return _default_entry(sole, cursor or text, config)

    # 6. 噪声（多仓库且无法定位归属）
    if last_unknown_alias is not None:
        return DispatchDecision(
            kind=DispatchKind.NOISE,
            reason=f"未找到名为 @{last_unknown_alias} 的仓库。请用 @ 加上配置中的仓库名/别名。",
        )
    return DispatchDecision(
        kind=DispatchKind.NOISE,
        reason="无法识别该消息属于哪个仓库。请用 @<repo-name> 前缀显式指定。",
    )
