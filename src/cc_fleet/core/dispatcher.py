"""消息分类：把进来的消息归到 command / new / continue / noise 四类。

规则按顺序：
0. text 以 `/` 起头 → 控制面指令（command）。优先于 quote 判定。
1. quote_text 中能解析出 [session: <slug> ...] 且 slug 在 storage 中且 ``is_open``
   （含 awaiting 与可恢复终态 FAILED/TIMEOUT/COMPLETED）→ CONTINUE。
2. text 以已知 `@<alias>` 起头（可能要先剥掉企微自动加的 `@<bot>`） → NEW。
   显式 @ 始终优先于 quote 里的隐式信息，体现用户意图。
3. text 没显式 @，但 quote 中能解析出 repo（无论来自完整 session tag 还是
   `[repo: ...]` repo-only tag）→ NEW，以 quote 中的 repo 当隐式 mention。
   适用于：quote 指向 CANCELLED 的 session、quote 里 slug 不存在于 storage、
   或 quote 只携带 repo-only tag。引用 CANCELLED 视为"放弃后想发新需求"。
4. text 中提到了某个 repo 的 keyword → 推断 repo，走 NEW。
5. 兜底：NOISE（机器人回一句提示，不开新 session）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from ..bot.message import IncomingMessage
from ..config.schema import AppConfig, RepoConfig
from ..util.ids import extract_quote_context


class DispatchKind(str, Enum):
    NEW = "new"
    CONTINUE = "continue"
    COMMAND = "command"
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
) -> DispatchDecision:
    """对一条消息进行分类。

    `is_open_session(slug)`：异步谓词，判断 slug 在 storage 中且 ``is_open``
    （含 awaiting 与可恢复终态 FAILED/TIMEOUT/COMPLETED）。
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
        normalized = _COMMAND_ALIASES.get(head.lower())
        if normalized is not None:
            arg = rest.strip() or None
            # /cancel / /plan / /resume 无参时回退到 quote 里的 slug,方便用户"引用某
            # session 消息发 /xxx"。
            if normalized in ("cancel", "plan", "resume") and arg is None:
                quote_ctx = extract_quote_context(quote)
                if quote_ctx.slug:
                    arg = quote_ctx.slug
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
            # 显式 @ 优先：忽略 quote 里的隐式信息
            return _new_decision(repo, rest)
        last_unknown_alias = alias
        cursor = rest

    # 3. text 无显式 @、quote 里能解析出 repo（slug 可能不存在、可能指向 CANCELLED）
    #    → 以 quote 中的 repo 作为隐式 mention，开新 session；text 整体作为需求
    #    FAILED/TIMEOUT/COMPLETED 走规则 1 CONTINUE，不会落到这里
    if ctx.repo:
        implicit_repo = config.repo_by_name_or_alias(ctx.repo)
        if implicit_repo is not None:
            return _new_decision(implicit_repo, cursor or text)

    # 4. 关键词兜底（用剥过 @ 之后的 cursor）
    fallback_text = cursor or text
    repo = _match_by_keyword(fallback_text, config.repos)
    if repo is not None:
        return _new_decision(repo, fallback_text)

    # 5. 噪声
    if last_unknown_alias is not None:
        return DispatchDecision(
            kind=DispatchKind.NOISE,
            reason=f"未找到名为 @{last_unknown_alias} 的仓库。请用 @ 加上配置中的仓库名/别名。",
        )
    return DispatchDecision(
        kind=DispatchKind.NOISE,
        reason="无法识别该消息属于哪个仓库。请用 @<repo-name> 前缀显式指定。",
    )
