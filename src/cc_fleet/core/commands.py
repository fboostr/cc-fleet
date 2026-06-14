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
from .state import WORKING_STATES, SessionState

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
_DEFAULT_VIEW_STATES: set[str] = {s.value for s in WORKING_STATES} | {
    SessionState.AWAITING_USER_CLARIFICATION.value,
}

# /plan 单条消息字符上限。企微 markdown 单条上限约 4K，这里以 4000 字符为切片阈值，
# 预留少量空间给分页头（"**plan [slug] (i/N)**\n\n"）。中文场景一字符 ~3 字节时，
# 4000 字符约 12K 字节，超过企微硬上限的概率仍然存在；如果实际遇到 plan 文件过大
# 被拒收，把这里调小到 1300 即可（中文场景下约对应 4K 字节）。
_PLAN_CHUNK_LIMIT = 4000


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
        "- `/cancel <slug>`：取消指定 session（也可引用某 session 消息发 `/cancel`，无需带参数）\n"
        "- `/resume <slug>`：显式拉起 working 状态的孤儿 session（主控曾被中断时留下的）；awaiting / 终态请用引用回复\n"
        "- `/plan <slug>`：查看指定 session 的 plan 文件全文（也可引用某 session 消息发 `/plan`，无需带参数）\n"
        "- `/repos`：列出当前配置的所有仓库及其 alias / keywords\n"
        "- `/help`：查看本帮助\n\n"
        f"超过 {_RECENT_DAYS} 天未活跃的 session 不在聊天里显示，请到本地网页端查看完整历史。\n\n"
        "**发起新需求**：`@<repo> 需求描述`\n"
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


def _split_for_chat(text: str, limit: int = _PLAN_CHUNK_LIMIT) -> list[str]:
    """把长文本切成不超过 ``limit`` 字符的多段，尽量在段落/行边界处切。

    策略：
    1. 整体 <= limit：原样返回单元素列表。
    2. 超长：贪心切片，先在 ``\\n\\n`` 段落边界回退，找不到再退到 ``\\n`` 行边界，
       最后退到硬切。每片末尾的空白裁掉、下一片开头同样跳过紧贴的空白。
    """
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        end = pos + limit
        if end >= n:
            chunks.append(text[pos:].rstrip())
            break
        # 优先在段落（\n\n）边界回退
        cut = text.rfind("\n\n", pos, end)
        if cut == -1 or cut <= pos:
            # 退到行边界
            cut = text.rfind("\n", pos, end)
        if cut == -1 or cut <= pos:
            # 没有合适边界，硬切
            cut = end
        chunks.append(text[pos:cut].rstrip())
        # 跳过紧贴的换行/空白，避免下一片开头一堆空行
        pos = cut
        while pos < n and text[pos] in ("\n", " ", "\t"):
            pos += 1
    # 极端情况下可能产生空 chunk（如开头就一堆空白），剔除
    return [c for c in chunks if c]


async def render_plan(
    db: Database,
    workspace_root: Path,
    arg: str | None,
) -> list[str]:
    """渲染 /plan 命令的应答。返回 list[str]：单条消息时长度为 1，超长时多条。

    slug 解析顺序：display_slug 优先，回退 internal slug（与 /cancel 一致）。
    路径与 ``Session._session_dir`` 对齐：``<workspace_root>/sessions/<internal>/plan.md``。
    所有状态的 session 都可查（含 CANCELLED），只要 plan.md 还在磁盘上。
    """
    if not arg:
        return [
            "用法：`/plan <slug>`，或引用某 session 的消息发 `/plan`（无需带参数）。"
        ]

    row = await db.get_session_by_display_slug(arg)
    if row is None:
        row = await db.get_session(arg)
    if row is None:
        return [f"未找到 session [{arg}]。"]

    internal = row["slug"]
    display = row.get("display_slug") or internal
    plan_path = (workspace_root / "sessions" / internal / "plan.md").expanduser()

    if not plan_path.exists():
        return [
            f"session [{display}] 暂无 plan 文件（路径：{plan_path}）。"
            "可能是 plan 阶段尚未跑过、或文件已被清理。"
        ]

    try:
        body = plan_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.exception("读取 plan 文件失败 slug=%s path=%s", internal, plan_path)
        return [f"读取 plan 文件失败：{e}"]

    if not body.strip():
        return [f"session [{display}] 的 plan 文件为空（路径：{plan_path}）。"]

    pieces = _split_for_chat(body)
    total = len(pieces)
    if total == 1:
        return pieces
    return [
        f"**plan [{display}] ({i}/{total})**\n\n{piece}"
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
