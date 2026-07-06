"""聊天控制面指令的具体实现。

支持：
- list（仅别名 /list；原 /status 已硬切移除）：
    - 默认仅展示最近 7 天内**活跃过**（``sessions.updated_at`` 在窗口内）、且处于
      "工作中 / 等待回复"（``WORKING_STATES`` ∪ ``{AWAITING_USER_CLARIFICATION}``）
      的 session；
    - `/list all` 展示最近 7 天内活跃过的所有状态 session（含 cancelled 与可恢复终态）；
    - 超过 7 天未活跃的 session 一律不在聊天里显示，请到本地网页端查看。
- help：可用指令说明
- cancel：取消指定 slug 的 session（复用 SessionManager.cancel）
- resume：显式拉起一个 working 状态的孤儿 session（主控被 kill 留下的 developing /
  planning / new / mr_submitting）,复用 SessionManager.resume_session
- plan：展示指定 session 的 plan 文件全文；超过 4K 字符自动拆多条消息
- repos：列出 ``config.yaml`` 当前配置的所有仓库 name / aliases / keywords / mode，
  方便用户知道 ``@<alias>`` 该填什么

输出 markdown 表格，列与 CLI sessions list 对齐；display_slug 优先于 internal。

返回类型说明：``dispatch_command`` 统一返回 ``list[str]``，上层按列表顺序逐条
``reply``；这样可以承载 ``/plan`` 这种需要拆多条发送的命令，其它单条命令包成
单元素列表即可。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config.schema import AppConfig
from ..storage.db import Database
from ..util.ids import format_session_tag
from ..util.text import split_for_chat, split_for_chat_with_tag
from .chat import _NO_REPO
from .state import CHAT_STATES, WORKING_STATES, SessionState

if TYPE_CHECKING:
    from .session_manager import SessionManager


logger = logging.getLogger(__name__)

# /list 默认/all 视图共用的"最近活跃"截断窗口（天）。判定依据是 ``sessions.updated_at``——
# 任何状态切换、澄清/followup、cancel、字段写入都会刷新它，所以等价于"最近 N 天内被动过"。
# 超过窗口未活跃的 session 一律不在聊天里显示，请到网页端查看完整历史。改这个值时记得
# 同步 render_help 与 README。
_RECENT_DAYS = 7

# /list 默认视图只展示这些状态：工作中（WORKING_STATES，含 reviewer 的 plan_reviewing /
# code_reviewing）+ 等待用户回复。从 WORKING_STATES 派生，避免新增工作态时这里漏更新。
# RESUMABLE_TERMINAL（FAILED/TIMEOUT/COMPLETED）虽然 is_open=True、可被引用回复唤醒，
# 但用户口径里不算"工作中或等待回复"，统一归 /list all 才看得到。
_DEFAULT_VIEW_STATES: set[str] = (
    {s.value for s in WORKING_STATES}
    | {SessionState.AWAITING_USER_CLARIFICATION.value}
    # chat 通道的 chatting / chat_awaiting 也算"工作中/等待回复"，默认视图可见。
    | {s.value for s in CHAT_STATES}
)

# /plan 单条消息字符上限。企微 markdown 单条上限约 4K，这里以 4000 字符为切片阈值，
# 预留少量空间给分页头（"**plan [slug] (i/N)**\n\n"）。中文场景一字符 ~3 字节时，
# 4000 字符约 12K 字节，超过企微硬上限的概率仍然存在；如果实际遇到 plan 文件过大
# 被拒收，把这里调小到 1300 即可（中文场景下约对应 4K 字节）。
_PLAN_CHUNK_LIMIT = 4000

# /plan 可查的三份文档（与 HTTP 面板的 Plan / Plan 审查 / Code 审查三个 tab 一一对应）。
# 用户输入的选择器关键词 -> 规范 doc key；省略选择器或写 "plan" 都回退到原始 plan。
# dispatcher 也会 import 本表，用来识别「引用 + 选择器」（如 `/plan review`）。
PLAN_SELECTOR_ALIASES: dict[str, str] = {
    "plan": "plan",
    "review": "plan-review",
    "plan-review": "plan-review",
    "code": "code-review",
    "code-review": "code-review",
}

# 规范 doc key -> (磁盘文件名, 面向用户的标签, 文件缺失时的补充说明)。文件名与落盘逻辑
# （Session._write_plan_md / _write_review_md）对齐；缺失说明沿用 HTTP 前端口径，便于
# 用户判断是没启用 Reviewer 还是阶段尚未产出。
_PLAN_DOCS: dict[str, tuple[str, str, str]] = {
    "plan": (
        "plan.md",
        "plan",
        "可能是 plan 阶段尚未跑过、或文件已被清理。",
    ),
    "plan-review": (
        "plan_review.md",
        "plan 审查",
        "未启用 Reviewer / 该阶段尚未产出。",
    ),
    "code-review": (
        "code_review.md",
        "code 审查",
        "未启用 Reviewer / 该阶段尚未产出。",
    ),
}


def _short_slug(row: dict[str, Any]) -> str:
    return row.get("display_slug") or row["slug"]


def _short_mr(url: str | None) -> str:
    """渲染 MR 列：有 URL 时输出 markdown 链接，方便企微端点击跳转。

    - 空值 → ``-``
    - GitLab 风格 ``.../merge_requests/<n>`` → ``[!<n>](<url>)``，沿用 GitLab 的短形态
    - 其它形态 URL → ``[link](<url>)`` 兜底，仍然可点
    """
    if not url:
        return "-"
    if "/merge_requests/" in url:
        tail = url.rsplit("/merge_requests/", 1)[-1]
        return f"[!{tail}]({url})"
    return f"[link]({url})"


def _parse_iso(ts: str | None) -> datetime | None:
    """容错解析 db 里写的 ISO8601 时间戳，失败返回 None。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _format_local_dt(ts: str | None) -> str:
    """把 ISO8601 时间戳转成本地时区的 ``YYYY-MM-DD HH:MM:SS``，给「最后活动」列用。

    无时区的串按 UTC 解析后再转本地时区，避免数据库里历史脏数据被错按本地时间。
    解析失败保持 ``-``，与原 _short_relative 的容错口径一致。
    """
    dt = _parse_iso(ts)
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _is_recent(row: dict[str, Any], now: datetime, days: int = _RECENT_DAYS) -> bool:
    """``updated_at`` 落在最近 ``days`` 天内才算 recent（即"最近活跃过"）。

    选用 ``updated_at`` 而非 ``created_at``：任何状态切换 / 澄清 / followup / cancel /
    字段写入都会刷新该列，所以它等价于"session 最近被动过"。

    解析失败的脏数据归类为"非最近"——保守剔除避免污染默认视图，写入日志便于追踪。
    """
    updated = _parse_iso(row.get("updated_at"))
    if updated is None:
        logger.warning(
            "无法解析 session updated_at slug=%s value=%r，按非最近剔除",
            row.get("slug"),
            row.get("updated_at"),
        )
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (now - updated) <= timedelta(days=days)


def _state_display(row: dict[str, Any], max_rounds: int) -> str:
    """awaiting 状态时把 clarify 轮数贴在后面，方便一眼看到澄清进度。"""
    state = row.get("state", "-")
    if state == "awaiting_user_clarification":
        rounds = row.get("clarify_rounds", 0)
        return f"{state} (r{rounds}/{max_rounds})"
    return state


def _render_table(title: str, rows: list[dict[str, Any]], max_clarify_rounds: int) -> str:
    if not rows:
        return f"**{title}**：（无）"
    lines = [
        f"**{title}**",
        "",
        "| slug | repo | state | 最后活动 | MR |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            "| `{slug}` | {repo} | {state} | {last_active} | {mr} |".format(
                slug=_short_slug(r),
                repo=r.get("repo", "-"),
                state=_state_display(r, max_clarify_rounds),
                last_active=_format_local_dt(r.get("updated_at")),
                mr=_short_mr(r.get("mr_url")),
            )
        )
    return "\n".join(lines)


async def render_list(
    db: Database,
    max_clarify_rounds: int,
    *,
    scope: str = "default",
) -> str:
    """渲染 /list 命令的应答文本（markdown）。

    - ``scope="default"``（``/list`` 无参）：仅展示最近 7 天内**活跃过**、且状态为
      工作中或等待用户回复的 session。
    - ``scope="all"``（``/list all``）：展示最近 7 天内活跃过的所有状态 session（含
      cancelled 与可恢复终态 FAILED/TIMEOUT/COMPLETED）。

    "活跃"以 ``sessions.updated_at`` 为准；``updated_at`` 距今超过 7 天（即最近 7 天
    未活跃）的 session 一律不会出现在聊天里——需要查看完整历史请打开本地网页端。
    """
    all_rows = await db.list_sessions()
    now = datetime.now(timezone.utc)
    recent_rows = [r for r in all_rows if _is_recent(r, now)]
    # list_sessions 已按 created_at DESC 排序，filter 后顺序保持

    hint = (
        f"_只展示最近 {_RECENT_DAYS} 天活跃过的 session，"
        "更早或长期未活跃的请到网页端查看。_"
    )

    if scope == "all":
        title = f"最近 {_RECENT_DAYS} 天活跃 session（{len(recent_rows)}）"
        body = _render_table(title, recent_rows, max_clarify_rounds)
        return f"{body}\n\n{hint}"

    # default
    rows = [r for r in recent_rows if r.get("state") in _DEFAULT_VIEW_STATES]
    title = f"工作中 / 等待回复 session（最近 {_RECENT_DAYS} 天活跃，{len(rows)}）"
    body = _render_table(title, rows, max_clarify_rounds)
    tip = (
        f"_输入 `/list all` 查看最近 {_RECENT_DAYS} 天活跃过的所有状态；"
        "更早或长期未活跃的请到网页端查看。_"
    )
    return f"{body}\n\n{tip}"


def render_help() -> str:
    return (
        "**可用指令**\n\n"
        f"- `/list`：列出最近 {_RECENT_DAYS} 天活跃过的工作中 / 等待回复 session\n"
        f"- `/list all`：列出最近 {_RECENT_DAYS} 天活跃过的所有状态 session（含已取消 / 已完成 / 失败 / 超时）\n"
        "- `/cancel <slug>`：**软取消**指定 session——不打断正在跑的 claude，让它体面收尾（也可引用某 session 消息发 `/cancel`，无需带参数）\n"
        "- `/kill <slug>`：**强杀**指定 session——立即杀掉正在跑的活进程再取消，用于 claude 卡死 / 跑飞不想再等（也可引用某 session 消息发 `/kill`）\n"
        "- `/resume <slug>`：显式拉起 working 状态的孤儿 session（主控曾被中断时留下的）；awaiting / 终态请用引用回复\n"
        "- `/plan <slug> [review|code]`：查看指定 session 的 plan 全文；`review`=plan 审查、`code`=code 审查、省略=原始 plan（也可引用某 session 消息发 `/plan [review|code]`，无需带 slug）\n"
        "- `/repos`：列出当前配置的所有仓库及其 alias / keywords\n"
        "- `/chat <消息>`：进入与 claude 的多轮**只读讨论**（读代码 / 理清需求，不改代码）；"
        "带 `@<repo>` 绑定仓库，单仓库时自动绑定唯一仓库，多仓库省略 @ 则用回退目录并给出警告\n"
        "- `/dev`：两种用法——**引用**一条 /chat 对话消息把讨论转成正式开发任务（复用对话上下文）；"
        "或 `/dev <需求>` 不引用直接开始开发（单仓库自动定位，多仓库用 `@<repo> /dev <需求>`）\n"
        "- `/help`：查看本帮助\n\n"
        f"超过 {_RECENT_DAYS} 天未活跃的 session 不在聊天里显示，请到本地网页端查看完整历史。\n\n"
        "**默认怎么用**：直接发消息即可（单仓库无需 @）——默认进入多轮只读讨论把需求聊清楚，"
        "然后**引用**该对话消息发 `/dev` 转成正式开发（走完整 plan→dev→MR 流水线）。"
        "该默认可用配置 `default_mode`（chat / dev）调整。\n"
        "**指定仓库**：配置了多个仓库时用 `@<repo> 消息` 指明；单仓库可省略 @。\n"
        "**一句话直达开发**：已经想清楚就发 `/dev <需求>`（或 `@<repo> /dev <需求>`），跳过讨论直接开发。\n"
        "**单需求开关 Reviewer**：需求里加 `[review]`（本次强制开审查）或 `[review:off]`（本次强制关），覆盖该仓库默认；标记会从需求中剥除\n"
        "**补充已有 session**：引用机器人含 `[session: <slug>]` 的消息回复即可"
    )


def _format_str_list(items: list[str]) -> str:
    """逗号拼接列表；空列表显示为 ``-``，与表格中其它"无值"格式一致。"""
    cleaned = [s for s in items if s]
    return ", ".join(cleaned) if cleaned else "-"


def render_repos(config: AppConfig) -> str:
    """渲染 /repos 命令的应答文本（markdown）。

    列出 ``config.yaml`` 中配置的所有仓库 name / aliases / keywords / mode，方便用户
    知道 ``@<alias>`` 该填什么、有哪些 keyword 可触发兜底路由。

    repos 数量一般 < 10，远低于单条消息 4K 字符上限，不分页。
    """
    repos = config.repos
    if not repos:
        return "**当前未配置任何仓库**（请检查 `config.yaml` 的 `repos` 段）。"
    lines = [
        f"**当前支持的仓库（{len(repos)}）**",
        "",
        "| name | aliases | keywords | mode |",
        "| --- | --- | --- | --- |",
    ]
    for r in repos:
        lines.append(
            "| {name} | {aliases} | {keywords} | {mode} |".format(
                name=r.name,
                aliases=_format_str_list(r.aliases),
                keywords=_format_str_list(r.keywords),
                mode=r.mode,
            )
        )
    return "\n".join(lines)


# `_split_for_chat` 的实现已迁到 util.text.split_for_chat（供 core/chat.py 等复用，避免
# 依赖本模块）。此处保留同名别名，兼容既有导入（如 tests/test_commands.py）。
_split_for_chat = split_for_chat


async def render_plan(
    db: Database,
    workspace_root: Path,
    arg: str | None,
) -> list[str]:
    """渲染 /plan 命令的应答。返回 list[str]：单条消息时长度为 1，超长时多条。

    支持文档选择器：``/plan <slug> [review|code]``。末尾 token 命中
    ``PLAN_SELECTOR_ALIASES`` 时取作选择器（review=plan 审查、code=code 审查），其余
    token 拼回 slug；省略选择器或写 ``plan`` 都回退到原始 ``plan.md``。引用某 session
    消息发 ``/plan [review|code]`` 时，slug 由 dispatcher 从 quote 注入到 arg 开头。

    slug 解析顺序：display_slug 优先，回退 internal slug（与 /cancel 一致）。
    路径与 ``Session._session_dir`` 对齐：``<workspace_root>/sessions/<internal>/<file>``。
    所有状态的 session 都可查（含 CANCELLED），只要对应文件还在磁盘上。
    """
    usage = (
        "用法：`/plan <slug> [review|code]`，或引用某 session 的消息发 "
        "`/plan [review|code]`（无需带 slug）。review=plan 审查，code=code 审查，"
        "省略=原始 plan。"
    )
    if not arg:
        return [usage]

    # 解析末尾的文档选择器；其余 token 拼回 slug。slug 不含空格，正常至多两段。
    parts = arg.split()
    selector = "plan"
    if len(parts) >= 2 and parts[-1].lower() in PLAN_SELECTOR_ALIASES:
        selector = PLAN_SELECTOR_ALIASES[parts[-1].lower()]
        parts = parts[:-1]
    elif (
        len(parts) == 1
        and parts[0].lower() in PLAN_SELECTOR_ALIASES
        and parts[0].lower() != "plan"
    ):
        # 直接 `/plan review` 但没给 slug（非引用场景）：提示需带 slug，不静默失败。
        return [usage]
    slug = " ".join(parts).strip()
    if not slug:
        return [usage]

    filename, label, missing_hint = _PLAN_DOCS[selector]

    row = await db.get_session_by_display_slug(slug)
    if row is None:
        row = await db.get_session(slug)
    if row is None:
        return [f"未找到 session [{slug}]。"]

    internal = row["slug"]
    display = row.get("display_slug") or internal
    doc_path = (workspace_root / "sessions" / internal / filename).expanduser()

    if not doc_path.exists():
        return [
            f"session [{display}] 暂无 {label} 文件（路径：{doc_path}）。{missing_hint}"
        ]

    try:
        body = doc_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.exception("读取 %s 文件失败 slug=%s path=%s", label, internal, doc_path)
        return [f"读取 {label} 文件失败：{e}"]

    if not body.strip():
        return [f"session [{display}] 的 {label} 文件为空（路径：{doc_path}）。"]

    # 每段都追加 session tag，使用户引用**任意一段** plan（含首/中段）都能反解出 session
    # 续聊，而非只有尾段可引用。分页头（"**label [display] (i/N)**\n\n"）会前置到每段，
    # 故用最长形态（99/99）预留其长度，连同 tag 一并从切分预算扣除，保证
    # "分页头 + 正文 + tag" 不超过单条上限。
    repo = row.get("repo")
    tag = "\n\n" + format_session_tag(
        display,
        repo=repo if repo and repo != _NO_REPO else None,
        claude_session_id=row.get("claude_session_id"),
    )
    header_margin = len(f"**{label} [{display}] (99/99)**\n\n")
    pieces = split_for_chat_with_tag(
        body, tag, _PLAN_CHUNK_LIMIT, extra_reserve=header_margin
    )
    total = len(pieces)
    if total == 1:
        return pieces
    return [
        f"**{label} [{display}] ({i}/{total})**\n\n{piece}"
        for i, piece in enumerate(pieces, start=1)
    ]


async def dispatch_command(
    db: Database,
    manager: "SessionManager",
    command: str,
    arg: str | None = None,
) -> list[str]:
    """统一入口；返回要逐条回给聊天的文本列表。未知命令在 dispatcher 已被拦截。

    多条返回主要服务 /plan（plan 文件可能超过单条消息上限）；其它命令始终返回
    单元素列表。
    """
    if command == "list":
        normalized_arg = (arg or "").strip().lower()
        if normalized_arg in ("", "all"):
            scope = "all" if normalized_arg == "all" else "default"
            return [
                await render_list(db, manager.config.pipeline.max_clarify_rounds, scope=scope)
            ]
        return [
            f"用法：`/list` 或 `/list all`（不识别参数：{arg}）。"
        ]
    if command == "help":
        return [render_help()]
    if command == "cancel":
        if not arg:
            return ["用法：`/cancel <slug>`，或引用某 session 的消息发 `/cancel`（无需带参数）。"]
        ok = await manager.cancel(arg)
        return [f"已取消 [{arg}]。" if ok else f"未找到未结案的 session [{arg}]。"]
    if command == "kill":
        if not arg:
            return ["用法：`/kill <slug>`，或引用某 session 的消息发 `/kill`（无需带参数）。"]
        ok = await manager.hard_cancel(arg)
        return [
            f"已强杀 [{arg}]（立即杀掉活进程并取消）。"
            if ok
            else f"未找到未结案的 session [{arg}]。"
        ]
    if command == "resume":
        if not arg:
            return [
                "用法：`/resume <slug>`,或引用某 session 的消息发 `/resume`（无需带参数）。"
            ]
        ok, text = await manager.resume_session(arg)
        return [text]
    if command == "plan":
        return await render_plan(db, manager.config.workspace_root, arg)
    if command == "repos":
        return [render_repos(manager.config)]
    return [f"未知命令：{command}"]
