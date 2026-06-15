"""聊天控制面指令 /list /help /cancel /plan 输出验证。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cc_fleet.config.schema import (
    AppConfig,
    ClaudeConfig,
    LimitsConfig,
    PipelineConfig,
    RepoConfig,
    WecomConfig,
)
from cc_fleet.core.commands import (
    _PLAN_CHUNK_LIMIT,
    _RECENT_DAYS,
    _split_for_chat,
    dispatch_command,
    render_help,
    render_list,
    render_plan,
    render_repos,
)
from cc_fleet.core.session_manager import SessionManager
from cc_fleet.core.state import SessionState
from cc_fleet.storage.db import Database


_MAX_ROUNDS = 5


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        pipeline=PipelineConfig(max_clarify_rounds=_MAX_ROUNDS),
        repos=[RepoConfig(name="demo", path=repo_root, default_branch="main")],
        limits=LimitsConfig(max_concurrent_sessions=2),
    )


@pytest.fixture
def manager(db: Database, cfg: AppConfig) -> SessionManager:
    async def _reply(_chatid: str, _text: str) -> None:
        return None
    return SessionManager(db, cfg, _reply)


async def _insert(
    db: Database,
    *,
    slug: str,
    state: SessionState,
    repo: str = "demo",
    display: str | None = None,
    mr: str | None = None,
    clarify_rounds: int = 0,
) -> None:
    await db.insert_session(
        {
            "slug": slug,
            "display_slug": display,
            "repo": repo,
            "state": state.value,
            "claude_session_id": None,
            "worktree_path": None,
            "branch": None,
            "default_branch": "main",
            "initial_request": "x",
            "chatid": "c",
            "userid": "u",
            "clarify_rounds": clarify_rounds,
            "last_error": None,
            "mr_url": mr,
        }
    )


async def _set_updated_at(db: Database, slug: str, dt: datetime) -> None:
    """覆盖某条 session 的 updated_at（即"最近活跃时间"），用于 7 天过滤窗口测试。"""
    await db.conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE slug = ?",
        (dt.astimezone().isoformat(), slug),
    )
    await db.conn.commit()


async def _set_created_and_updated_at(db: Database, slug: str, dt: datetime) -> None:
    """同时把 created_at 与 updated_at 拨到指定时间，模拟"老建且之后没动过"的 session。"""
    ts = dt.astimezone().isoformat()
    await db.conn.execute(
        "UPDATE sessions SET created_at = ?, updated_at = ? WHERE slug = ?",
        (ts, ts, slug),
    )
    await db.conn.commit()


# ---------- /list 默认视图 ----------


async def test_list_default_empty(db: Database):
    text = await render_list(db, _MAX_ROUNDS)
    # 表头仍渲染，只是行数为 0；提示文案存在
    assert "工作中 / 等待回复 session" in text
    assert "0" in text
    # 默认视图的引导
    assert "/list all" in text


async def test_list_default_only_working_and_awaiting(db: Database):
    """默认视图只显示 working + awaiting；CANCELLED 与可恢复终态 (COMPLETED/FAILED/TIMEOUT) 均不出现。"""
    await _insert(db, slug="tmp-w", state=SessionState.DEVELOPING, display="work-feat")
    await _insert(
        db,
        slug="tmp-a",
        state=SessionState.AWAITING_USER_CLARIFICATION,
        display="ask-clarify",
        clarify_rounds=2,
    )
    await _insert(db, slug="tmp-c", state=SessionState.COMPLETED, display="done-feat")
    await _insert(db, slug="tmp-f", state=SessionState.FAILED, display="failed-feat")
    await _insert(db, slug="tmp-t", state=SessionState.TIMEOUT, display="timeout-feat")
    await _insert(db, slug="tmp-x", state=SessionState.CANCELLED, display="cancel-feat")

    text = await render_list(db, _MAX_ROUNDS)
    assert "work-feat" in text
    assert "ask-clarify" in text
    # awaiting 仍带 (rN/M) 进度
    assert "awaiting_user_clarification (r2/5)" in text
    # 其它状态都不应出现
    for absent in ("done-feat", "failed-feat", "timeout-feat", "cancel-feat"):
        assert absent not in text
    # 标题计数为 2
    assert "工作中 / 等待回复 session（最近 7 天活跃，2）" in text


async def test_list_default_filters_out_inactive_session(db: Database):
    """updated_at 超 7 天（即最近 7 天没活跃过）的 working session 不出现在默认视图。"""
    await _insert(db, slug="tmp-recent", state=SessionState.DEVELOPING, display="fresh")
    await _insert(db, slug="tmp-old", state=SessionState.DEVELOPING, display="stale")
    await _set_created_and_updated_at(
        db,
        "tmp-old",
        datetime.now(timezone.utc) - timedelta(days=_RECENT_DAYS + 1),
    )

    text = await render_list(db, _MAX_ROUNDS)
    assert "fresh" in text
    assert "stale" not in text
    assert "工作中 / 等待回复 session（最近 7 天活跃，1）" in text


async def test_list_default_keeps_recently_active_old_session(db: Database):
    """关键回归：created_at 很久以前、但 updated_at 在 7 天内的 session 应被显示。"""
    await _insert(db, slug="tmp-resurrected", state=SessionState.DEVELOPING, display="resurrected")
    # 先把 created_at 拨到 30 天前
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    await db.conn.execute(
        "UPDATE sessions SET created_at = ? WHERE slug = ?",
        (long_ago.astimezone().isoformat(), "tmp-resurrected"),
    )
    await db.conn.commit()
    # 再把 updated_at 拨到 1 天前，模拟"老 session 最近被动过"
    await _set_updated_at(
        db,
        "tmp-resurrected",
        datetime.now(timezone.utc) - timedelta(days=1),
    )

    text = await render_list(db, _MAX_ROUNDS)
    assert "resurrected" in text
    assert "工作中 / 等待回复 session（最近 7 天活跃，1）" in text


# ---------- /list all 视图 ----------


async def test_list_all_includes_all_states(db: Database):
    """all 视图展示最近 7 天内活跃过的所有状态 session。"""
    await _insert(db, slug="tmp-w", state=SessionState.PLANNING, display="planning-feat")
    await _insert(db, slug="tmp-a", state=SessionState.AWAITING_USER_CLARIFICATION, display="await-feat")
    await _insert(db, slug="tmp-c", state=SessionState.COMPLETED, display="completed-feat")
    await _insert(db, slug="tmp-f", state=SessionState.FAILED, display="failed-feat")
    await _insert(db, slug="tmp-x", state=SessionState.CANCELLED, display="cancelled-feat")

    text = await render_list(db, _MAX_ROUNDS, scope="all")
    for display in (
        "planning-feat",
        "await-feat",
        "completed-feat",
        "failed-feat",
        "cancelled-feat",
    ):
        assert display in text
    assert "最近 7 天活跃 session（5）" in text


async def test_list_all_filters_out_inactive(db: Database):
    """all 视图同样受 7 天活跃窗口约束；最近 7 天未活跃的 session 一律不在聊天里显示。"""
    await _insert(db, slug="tmp-recent", state=SessionState.CANCELLED, display="recent-cancel")
    await _insert(db, slug="tmp-old", state=SessionState.CANCELLED, display="old-cancel")
    await _set_created_and_updated_at(
        db,
        "tmp-old",
        datetime.now(timezone.utc) - timedelta(days=_RECENT_DAYS + 2),
    )

    text = await render_list(db, _MAX_ROUNDS, scope="all")
    assert "recent-cancel" in text
    assert "old-cancel" not in text


async def test_list_all_no_truncation(db: Database):
    """all 视图不再像旧 /status 那样把已终结截至 5 条——10 条 cancelled 都应出现。"""
    for i in range(10):
        await _insert(db, slug=f"done-{i}", state=SessionState.CANCELLED, display=f"d{i}")
    text = await render_list(db, _MAX_ROUNDS, scope="all")
    for i in range(10):
        assert f"| `d{i}` |" in text
    assert "最近 7 天活跃 session（10）" in text


async def test_list_all_hint_about_web_panel(db: Database):
    """all 视图末尾应明确提示超过 7 天未活跃的需到网页端查看。"""
    text = await render_list(db, _MAX_ROUNDS, scope="all")
    assert "网页端" in text


# ---------- dispatch_command 路由 ----------


async def test_dispatch_command_list_default(db: Database, manager: SessionManager):
    out = await dispatch_command(db, manager, "list", None)
    assert isinstance(out, list) and len(out) == 1
    assert "工作中 / 等待回复 session" in out[0]


async def test_dispatch_command_list_all(db: Database, manager: SessionManager):
    out = await dispatch_command(db, manager, "list", "all")
    assert isinstance(out, list) and len(out) == 1
    assert "最近 7 天活跃 session" in out[0]


async def test_dispatch_command_list_all_case_insensitive(db: Database, manager: SessionManager):
    """dispatcher 不做 lower，由 commands 层兜底大小写。"""
    out = await dispatch_command(db, manager, "list", "ALL")
    assert "最近 7 天活跃 session" in out[0]


async def test_dispatch_command_list_bad_arg(db: Database, manager: SessionManager):
    out = await dispatch_command(db, manager, "list", "garbage")
    assert len(out) == 1
    assert "用法" in out[0] and "/list" in out[0]


async def test_dispatch_command_help(db: Database, manager: SessionManager):
    out = await dispatch_command(db, manager, "help", None)
    assert isinstance(out, list) and len(out) == 1
    assert "可用指令" in out[0]
    # 帮助中应当列出 /list、/list all、/cancel、/plan
    assert "/list" in out[0]
    assert "/list all" in out[0]
    assert "/cancel" in out[0]
    assert "/plan" in out[0]


async def test_dispatch_cancel_without_arg(db: Database, manager: SessionManager):
    out = await dispatch_command(db, manager, "cancel", None)
    assert len(out) == 1
    assert "用法" in out[0] and "/cancel" in out[0]


async def test_dispatch_cancel_unknown_slug(db: Database, manager: SessionManager):
    out = await dispatch_command(db, manager, "cancel", "no-such-slug")
    assert len(out) == 1 and "未找到" in out[0]


async def test_dispatch_cancel_existing_session(db: Database, manager: SessionManager):
    # 直接 insert 一个 PLANNING 状态的 session（没有内存 ctx，走 db fallback 分支）
    await _insert(db, slug="tmp-x", state=SessionState.PLANNING, display="add-x")
    out = await dispatch_command(db, manager, "cancel", "add-x")
    assert len(out) == 1 and "已取消" in out[0]
    row = await db.get_session("tmp-x")
    assert row["state"] == SessionState.CANCELLED.value


async def test_dispatch_resume_without_arg(db: Database, manager: SessionManager):
    out = await dispatch_command(db, manager, "resume", None)
    assert len(out) == 1
    assert "用法" in out[0] and "/resume" in out[0]


async def test_dispatch_resume_unknown_slug(db: Database, manager: SessionManager):
    out = await dispatch_command(db, manager, "resume", "no-such-slug")
    assert len(out) == 1 and "未找到" in out[0]


async def test_dispatch_resume_awaiting_returns_hint(db: Database, manager: SessionManager):
    """awaiting 状态 /resume 应被拒绝并提示用户用引用回复。"""
    await _insert(db, slug="tmp-aw", state=SessionState.AWAITING_USER_CLARIFICATION, display="aw-x")
    out = await dispatch_command(db, manager, "resume", "aw-x")
    assert len(out) == 1
    assert "引用" in out[0] and "澄清" in out[0]


def test_help_text_lists_new_commands():
    text = render_help()
    # 旧 /status 必须不再出现
    assert "/status" not in text
    # 新指令齐全
    assert "/list" in text
    assert "/list all" in text
    assert "/help" in text
    assert "/cancel" in text
    assert "/resume" in text
    assert "/plan" in text
    assert "/repos" in text
    # 文案明确"超过 7 天到网页端查看"
    assert "网页端" in text


# ---------- /repos ----------


def _make_repos_cfg(tmp_path: Path, repos: list[RepoConfig]) -> AppConfig:
    return AppConfig(
        workspace_root=tmp_path / "ws",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "state.db",
        wecom=WecomConfig(bot_id="x", bot_secret="y"),
        claude=ClaudeConfig(),
        pipeline=PipelineConfig(max_clarify_rounds=_MAX_ROUNDS),
        repos=repos,
        limits=LimitsConfig(max_concurrent_sessions=2),
    )


def test_render_repos_basic(tmp_path: Path):
    """name / aliases / keywords / mode 四列均正确渲染。"""
    repo_root = tmp_path / "repo-a"
    repo_root.mkdir()
    cfg = _make_repos_cfg(
        tmp_path,
        [
            RepoConfig(
                name="demo-repo",
                aliases=["demo", "d"],
                path=repo_root,
                default_branch="main",
                keywords=["演示", "demo-kw"],
            )
        ],
    )
    text = render_repos(cfg)
    assert "当前支持的仓库（1）" in text
    assert "| name | aliases | keywords | mode |" in text
    assert "| demo-repo | demo, d | 演示, demo-kw | local |" in text


def test_render_repos_empty_aliases_keywords(tmp_path: Path):
    """aliases / keywords 为空时显示为 ``-``，与表格其它"无值"格式一致。"""
    repo_root = tmp_path / "repo-b"
    repo_root.mkdir()
    cfg = _make_repos_cfg(
        tmp_path,
        [
            RepoConfig(
                name="bare-repo",
                aliases=[],
                path=repo_root,
                default_branch="main",
                keywords=[],
            )
        ],
    )
    text = render_repos(cfg)
    assert "| bare-repo | - | - | local |" in text


def test_render_repos_includes_remote_mode(tmp_path: Path):
    """mode=remote 的 repo 也应正确出现，mode 列显示为 ``remote``。"""
    local_root = tmp_path / "repo-local"
    local_root.mkdir()
    remote_shell = tmp_path / "repo-remote"
    remote_shell.mkdir()
    cfg = _make_repos_cfg(
        tmp_path,
        [
            RepoConfig(
                name="local-repo",
                aliases=["loc"],
                path=local_root,
                default_branch="main",
                keywords=[],
            ),
            RepoConfig(
                name="remote-repo",
                aliases=["rem"],
                path=remote_shell,
                default_branch="main",
                keywords=["远端"],
                mode="remote",
                remote_ssh_alias="dev-box",
                remote_repo_path="/home/x/remote-repo",
                remote_worktree_root="/home/x/remote-repo-worktrees",
            ),
        ],
    )
    text = render_repos(cfg)
    assert "当前支持的仓库（2）" in text
    assert "| local-repo | loc | - | local |" in text
    assert "| remote-repo | rem | 远端 | remote |" in text


async def test_dispatch_command_repos(
    db: Database, manager: SessionManager
):
    """/repos 通过 dispatch_command 路由能拿到非空表格。"""
    out = await dispatch_command(db, manager, "repos", None)
    assert isinstance(out, list) and len(out) == 1
    # cfg fixture 中已注入一个 name=demo 的 repo
    assert "当前支持的仓库" in out[0]
    assert "| demo |" in out[0]


# ---------- /plan ----------


def _write_plan(workspace_root: Path, internal_slug: str, body: str) -> Path:
    """模拟 Session._write_plan_md：在 workspace_root/sessions/<slug>/ 下写 plan.md。"""
    return _write_doc(workspace_root, internal_slug, "plan.md", body)


def _write_doc(
    workspace_root: Path, internal_slug: str, filename: str, body: str
) -> Path:
    """在 workspace_root/sessions/<slug>/ 下写任意文档（plan.md / plan_review.md /
    code_review.md），模拟 Session 的落盘行为。"""
    sdir = workspace_root / "sessions" / internal_slug
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / filename
    path.write_text(body, encoding="utf-8")
    return path


async def test_plan_without_arg(db: Database, cfg: AppConfig):
    out = await render_plan(db, cfg.workspace_root, None)
    assert len(out) == 1
    assert "用法" in out[0] and "/plan" in out[0]


async def test_plan_unknown_slug(db: Database, cfg: AppConfig):
    out = await render_plan(db, cfg.workspace_root, "no-such")
    assert len(out) == 1 and "未找到" in out[0]


async def test_plan_session_without_file(db: Database, cfg: AppConfig):
    await _insert(db, slug="tmp-empty", state=SessionState.NEW, display="empty-plan")
    out = await render_plan(db, cfg.workspace_root, "empty-plan")
    assert len(out) == 1
    assert "暂无 plan" in out[0]
    assert "empty-plan" in out[0]


async def test_plan_empty_file(db: Database, cfg: AppConfig):
    await _insert(db, slug="tmp-blank", state=SessionState.PLANNING, display="blank-plan")
    _write_plan(cfg.workspace_root, "tmp-blank", "   \n\n  ")
    out = await render_plan(db, cfg.workspace_root, "blank-plan")
    assert len(out) == 1 and "plan 文件为空" in out[0]


async def test_plan_short_single_message(db: Database, cfg: AppConfig):
    await _insert(db, slug="tmp-short", state=SessionState.PLANNING, display="short-plan")
    _write_plan(cfg.workspace_root, "tmp-short", "# 标题\n\n这是一个短 plan。")
    out = await render_plan(db, cfg.workspace_root, "short-plan")
    assert len(out) == 1
    # 单条消息不带分页头
    assert out[0].startswith("# 标题")
    assert "短 plan" in out[0]


async def test_plan_lookup_by_internal_slug(db: Database, cfg: AppConfig):
    """display_slug 未命中时按 internal slug 兜底。"""
    await _insert(db, slug="tmp-internal", state=SessionState.PLANNING, display=None)
    _write_plan(cfg.workspace_root, "tmp-internal", "内部 slug 路径")
    out = await render_plan(db, cfg.workspace_root, "tmp-internal")
    assert len(out) == 1 and "内部 slug 路径" in out[0]
    # 此时 display_slug 缺省，分页头/提示都会回退到 internal
    # 单条不带分页头，直接验证内容即可


async def test_plan_long_is_split(db: Database, cfg: AppConfig):
    """plan 文件超过 4K 字符时按段落切多条；每条带分页头，每条长度合理。"""
    await _insert(db, slug="tmp-long", state=SessionState.PLANNING, display="long-plan")
    # 构造一个超过 _PLAN_CHUNK_LIMIT 的文件，由若干个段落组成
    paragraph = "段落内容 " * 80  # ~640 字符（含空格）
    body_parts = [f"## 第 {i} 节\n\n{paragraph}" for i in range(20)]
    body = "\n\n".join(body_parts)
    assert len(body) > _PLAN_CHUNK_LIMIT  # 前提
    _write_plan(cfg.workspace_root, "tmp-long", body)

    out = await render_plan(db, cfg.workspace_root, "long-plan")
    assert len(out) >= 2, "超长 plan 应该被拆成多条"
    # 每条都带分页头
    for i, piece in enumerate(out, start=1):
        assert piece.startswith(f"**plan [long-plan] ({i}/{len(out)})**")
    # 分页头之外的实际内容长度不应超出 limit 太多（容差给分页头）
    for piece in out:
        assert len(piece) <= _PLAN_CHUNK_LIMIT + 100


async def test_dispatch_plan_routes_through_dispatch_command(
    db: Database, manager: SessionManager, cfg: AppConfig
):
    await _insert(db, slug="tmp-route", state=SessionState.PLANNING, display="route-plan")
    _write_plan(cfg.workspace_root, "tmp-route", "plan 正文 via dispatch")
    out = await dispatch_command(db, manager, "plan", "route-plan")
    assert isinstance(out, list)
    assert len(out) == 1
    assert "plan 正文 via dispatch" in out[0]


# ---------- /plan 文档选择器（review / code） ----------


async def test_plan_review_selector_reads_plan_review(db: Database, cfg: AppConfig):
    """`/plan <slug> review` 读 plan_review.md，而非 plan.md。"""
    await _insert(db, slug="tmp-rev", state=SessionState.PLANNING, display="rev-plan")
    _write_plan(cfg.workspace_root, "tmp-rev", "原始 plan 正文")
    _write_doc(cfg.workspace_root, "tmp-rev", "plan_review.md", "Plan 审查意见正文")
    out = await render_plan(db, cfg.workspace_root, "rev-plan review")
    assert len(out) == 1
    assert "Plan 审查意见正文" in out[0]
    assert "原始 plan 正文" not in out[0]


async def test_plan_code_selector_reads_code_review(db: Database, cfg: AppConfig):
    """`/plan <slug> code` 读 code_review.md。"""
    await _insert(db, slug="tmp-code", state=SessionState.DEVELOPING, display="code-plan")
    _write_doc(cfg.workspace_root, "tmp-code", "code_review.md", "Code 审查意见正文")
    out = await render_plan(db, cfg.workspace_root, "code-plan code")
    assert len(out) == 1
    assert "Code 审查意见正文" in out[0]


async def test_plan_explicit_plan_selector_reads_plan(db: Database, cfg: AppConfig):
    """`/plan <slug> plan` 显式选 plan，与省略选择器等价。"""
    await _insert(db, slug="tmp-exp", state=SessionState.PLANNING, display="exp-plan")
    _write_plan(cfg.workspace_root, "tmp-exp", "原始 plan via explicit")
    out = await render_plan(db, cfg.workspace_root, "exp-plan plan")
    assert len(out) == 1
    assert "原始 plan via explicit" in out[0]


async def test_plan_review_missing_file(db: Database, cfg: AppConfig):
    """未启用 Reviewer / 审查文件未产出时，给明确提示而非空白。"""
    await _insert(db, slug="tmp-norev", state=SessionState.PLANNING, display="norev-plan")
    _write_plan(cfg.workspace_root, "tmp-norev", "只有原始 plan")
    out = await render_plan(db, cfg.workspace_root, "norev-plan review")
    assert len(out) == 1
    assert "暂无 plan 审查" in out[0]
    assert "未启用 Reviewer" in out[0]


async def test_plan_selector_without_slug_returns_usage(db: Database, cfg: AppConfig):
    """直接 `/plan review`（无引用、无 slug）→ 用法提示，不静默失败。"""
    out = await render_plan(db, cfg.workspace_root, "review")
    assert len(out) == 1
    assert "用法" in out[0]


async def test_plan_review_selector_long_split_uses_label(db: Database, cfg: AppConfig):
    """选择器场景下超长分页头用对应文档标签（plan 审查），而非写死的 plan。"""
    await _insert(db, slug="tmp-revlong", state=SessionState.PLANNING, display="revlong")
    paragraph = "审查内容 " * 80
    body = "\n\n".join(f"## 第 {i} 节\n\n{paragraph}" for i in range(20))
    assert len(body) > _PLAN_CHUNK_LIMIT
    _write_doc(cfg.workspace_root, "tmp-revlong", "plan_review.md", body)
    out = await render_plan(db, cfg.workspace_root, "revlong review")
    assert len(out) >= 2
    for i, piece in enumerate(out, start=1):
        assert piece.startswith(f"**plan 审查 [revlong] ({i}/{len(out)})**")


# ---------- _split_for_chat ----------


def test_split_for_chat_short_returns_single():
    assert _split_for_chat("abc") == ["abc"]


def test_split_for_chat_respects_paragraph_boundary():
    # 限制 50，三段，每段 30 字符 → 段落边界切，应该 2-3 块
    body = ("a" * 30 + "\n\n" + "b" * 30 + "\n\n" + "c" * 30)
    pieces = _split_for_chat(body, limit=50)
    assert len(pieces) >= 2
    # 第一块应是"a"那段（在 \n\n 边界回退）
    assert pieces[0] == "a" * 30


def test_split_for_chat_hard_cut_when_no_boundary():
    # 没有任何换行的超长串，必须硬切；且不应丢内容
    body = "x" * 100
    pieces = _split_for_chat(body, limit=30)
    assert "".join(pieces) == body


async def test_list_renders_mr_url_as_markdown_link(db: Database):
    """有 mr_url 的 session 在表格里应渲染成可点击的 markdown 链接 `[!<n>](<url>)`。"""
    url = "https://gitlab.example.com/g/r/-/merge_requests/42"
    await _insert(
        db,
        slug="tmp-mr",
        state=SessionState.MR_SUBMITTING,
        display="mr-link",
        mr=url,
    )
    text = await render_list(db, _MAX_ROUNDS)
    assert f"[!42]({url})" in text


async def test_list_renders_last_active_time_to_seconds(db: Database):
    """「最后活动」列应当读 updated_at，并渲染成本地时区的 `YYYY-MM-DD HH:MM:SS`。"""
    import re

    await _insert(db, slug="tmp-active", state=SessionState.DEVELOPING, display="active-feat")
    # 用一个明确的 UTC 时刻覆盖 updated_at，预期输出落到本地时区的对应字符串
    fixed = datetime(2026, 5, 19, 10, 30, 45, tzinfo=timezone.utc)
    await _set_updated_at(db, "tmp-active", fixed)

    text = await render_list(db, _MAX_ROUNDS)
    # 表头列名同步更新为「最后活动」
    assert "最后活动" in text
    # 表格里精确到秒的本地时间字符串
    expected = fixed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert expected in text
    # 兜底正则保证格式形态稳定
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)
