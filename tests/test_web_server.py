"""HTTP 面板 GET 接口的端到端验证（含 plan / plan-review / code-review 三份 markdown）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cc_fleet.config.schema import HttpConfig
from cc_fleet.core.state import SessionState
from cc_fleet.storage.db import Database
from cc_fleet.web.server import WebServer


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
async def workspace_root(tmp_path: Path) -> Path:
    """plan.md 落在 ``<workspace_root>/sessions/<slug>/plan.md``。
    复用 db fixture 的 tmp_path，让 db 与 plan 文件在同一根目录下，模拟真实部署。"""
    return tmp_path


@pytest.fixture
async def client(db: Database, workspace_root: Path) -> TestClient:
    web = WebServer(
        db,
        HttpConfig(enabled=True, bind="127.0.0.1", port=0),
        workspace_root,
    )
    app = web._build_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


async def _seed(db: Database) -> None:
    await db.insert_session(
        {
            "slug": "tmp-a",
            "display_slug": "add-readme",
            "repo": "demo",
            "state": SessionState.DEVELOPING.value,
            "claude_session_id": None,
            "worktree_path": None,
            "branch": None,
            "default_branch": "main",
            "initial_request": "加 readme",
            "chatid": "c",
            "userid": "u",
            "clarify_rounds": 0,
            "last_error": None,
            "mr_url": None,
        }
    )
    await db.insert_session(
        {
            "slug": "tmp-b",
            "display_slug": "old-feature",
            "repo": "demo",
            "state": SessionState.COMPLETED.value,
            "claude_session_id": None,
            "worktree_path": None,
            "branch": None,
            "default_branch": "main",
            "initial_request": "x",
            "chatid": "c",
            "userid": "u",
            "clarify_rounds": 0,
            "last_error": None,
            "mr_url": "https://gitlab/example/-/merge_requests/9",
        }
    )
    await db.add_message("tmp-a", "in", "请加 readme")
    await db.add_message("tmp-a", "out", "已收到")
    await db.add_event("tmp-a", "claude.system", {"type": "system", "subtype": "init", "session_id": "sid-1"})
    await db.add_event("tmp-a", "claude.assistant", {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})


async def test_list_sessions(client: TestClient, db: Database):
    await _seed(db)
    resp = await client.get("/api/sessions")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["sessions"]) == 2


async def test_list_sessions_filter_open(client: TestClient, db: Database):
    """改动后 is_open 含 RESUMABLE_TERMINAL（failed/timeout/completed）→ COMPLETED
    也算 open。seed 的两条（DEVELOPING + COMPLETED）都应被 ?state=open 返回。"""
    await _seed(db)
    resp = await client.get("/api/sessions?state=open")
    data = await resp.json()
    slugs = sorted(s["display_slug"] for s in data["sessions"])
    assert slugs == ["add-readme", "old-feature"]


async def test_detail_by_display_slug(client: TestClient, db: Database):
    await _seed(db)
    resp = await client.get("/api/sessions/add-readme")
    assert resp.status == 200
    data = await resp.json()
    assert data["session"]["slug"] == "tmp-a"


async def test_detail_not_found(client: TestClient):
    resp = await client.get("/api/sessions/no-such-thing")
    assert resp.status == 404


async def test_events_decoded(client: TestClient, db: Database):
    await _seed(db)
    resp = await client.get("/api/sessions/add-readme/events")
    data = await resp.json()
    assert len(data["events"]) == 2
    # payload_json 被解析回 payload
    first = data["events"][0]
    assert "payload" in first and isinstance(first["payload"], dict)


async def test_events_excludes_stream_event(client: TestClient, db: Database):
    """claude SDK 的 ``*.stream_event`` 碎片应在 SQL 层就被排除。

    回归用例：曾经因为 list_events 不过滤 stream_event，HTTP 面板 Events 列
    实际只能看到尾部少量可见事件——配额被流式碎片吃光。
    """
    await _seed(db)
    # 在 seed 的 2 条可见事件之外，再塞一堆 stream_event 碎片
    for _ in range(50):
        await db.add_event(
            "tmp-a",
            "claude.stream_event",
            {"type": "stream_event", "event": {"type": "content_block_delta"}},
        )
    await db.add_event(
        "tmp-a",
        "claude.assistant",
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "tail"}]}},
    )
    resp = await client.get("/api/sessions/add-readme/events")
    data = await resp.json()
    kinds = [e["kind"] for e in data["events"]]
    assert all(not k.endswith(".stream_event") for k in kinds), kinds
    # 原本 2 条 seed + 新追加 1 条 assistant，stream_event 全部被剔除 → 共 3 条
    assert len(data["events"]) == 3


async def test_messages(client: TestClient, db: Database):
    await _seed(db)
    resp = await client.get("/api/sessions/add-readme/messages")
    data = await resp.json()
    assert [m["direction"] for m in data["messages"]] == ["in", "out"]


async def test_index_served(client: TestClient):
    resp = await client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert "cc-fleet" in body.lower() or "sessions" in body.lower()


async def _write_md(workspace_root: Path, slug: str, filename: str, body: str) -> Path:
    sdir = workspace_root / "sessions" / slug
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / filename
    path.write_text(body, encoding="utf-8")
    return path


async def _write_plan(workspace_root: Path, slug: str, body: str) -> Path:
    return await _write_md(workspace_root, slug, "plan.md", body)


async def test_plan_returns_markdown(
    client: TestClient, db: Database, workspace_root: Path
):
    await _seed(db)
    body = "# 标题\n\n正文段落。"
    path = await _write_plan(workspace_root, "tmp-a", body)
    # 通过 display_slug 访问也应能命中
    resp = await client.get("/api/sessions/add-readme/plan")
    assert resp.status == 200
    data = await resp.json()
    assert data["plan"] == body
    assert data["path"] == str(path)
    assert data["mtime"]


async def test_plan_internal_slug(
    client: TestClient, db: Database, workspace_root: Path
):
    """internal slug 也能直接命中。"""
    await _seed(db)
    body = "## 子标题"
    await _write_plan(workspace_root, "tmp-a", body)
    resp = await client.get("/api/sessions/tmp-a/plan")
    assert resp.status == 200
    data = await resp.json()
    assert data["plan"] == body


async def test_plan_no_file(client: TestClient, db: Database):
    """session 存在但 plan.md 还没产出 → 404 / no_plan。"""
    await _seed(db)
    resp = await client.get("/api/sessions/add-readme/plan")
    assert resp.status == 404
    data = await resp.json()
    assert data["error"] == "no_plan"


async def test_plan_unknown_slug(client: TestClient):
    """未注册 slug → 404 / not_found。"""
    resp = await client.get("/api/sessions/no-such-thing/plan")
    assert resp.status == 404
    data = await resp.json()
    assert data["error"] == "not_found"


async def test_plan_review_returns_markdown(
    client: TestClient, db: Database, workspace_root: Path
):
    """plan_review.md 通过 display_slug 也能命中，正文字段沿用 ``plan`` 复用前端逻辑。"""
    await _seed(db)
    body = "审查意见：plan 边界没覆盖空输入。\n\nREVIEW_VERDICT: NEEDS_REVISION"
    path = await _write_md(workspace_root, "tmp-a", "plan_review.md", body)
    resp = await client.get("/api/sessions/add-readme/plan-review")
    assert resp.status == 200
    data = await resp.json()
    assert data["plan"] == body
    assert data["path"] == str(path)


async def test_plan_review_no_file(client: TestClient, db: Database):
    """未启用 Reviewer / 该阶段未产出 → 404 / no_plan_review。"""
    await _seed(db)
    resp = await client.get("/api/sessions/add-readme/plan-review")
    assert resp.status == 404
    data = await resp.json()
    assert data["error"] == "no_plan_review"


async def test_code_review_returns_markdown(
    client: TestClient, db: Database, workspace_root: Path
):
    await _seed(db)
    body = "代码审查：x.py 第 12 行漏了 None 校验。"
    path = await _write_md(workspace_root, "tmp-a", "code_review.md", body)
    resp = await client.get("/api/sessions/tmp-a/code-review")
    assert resp.status == 200
    data = await resp.json()
    assert data["plan"] == body
    assert data["path"] == str(path)


async def test_code_review_no_file(client: TestClient, db: Database):
    await _seed(db)
    resp = await client.get("/api/sessions/add-readme/code-review")
    assert resp.status == 404
    data = await resp.json()
    assert data["error"] == "no_code_review"
