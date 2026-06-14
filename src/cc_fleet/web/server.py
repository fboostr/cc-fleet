"""基于 aiohttp 的本地只读 HTTP 面板。

设计要点（见 plan A3/A7/A8）：
- 默认 bind=127.0.0.1，无鉴权。仅本机进程使用。
- 共用 App 的 Database 单连接（aiosqlite + WAL；只读查询并发安全）。
- 静态前端是 web/static/index.html 一个单文件，原生 JS + setInterval 轮询。
- 不提供任何写操作，避免误点丢活。

路由：
  GET /                            返回首页 HTML
  GET /api/sessions[?state=open]   sessions 列表。state=open 走 ``is_open`` 谓词：
                                   工作中 + awaiting + 可恢复终态（completed/failed/timeout）；
                                   只有 cancelled 不算 open。其它值按 state 字面量精确匹配。
  GET /api/sessions/{slug}         单 session 详情
  GET /api/sessions/{slug}/events  最近 N 条事件（倒序，默认 500，上限 5000）。
                                   ``*.stream_event`` 已在 SQL 层排除，避免流式
                                   碎片把配额吃光导致早段可见事件被截断。
  GET /api/sessions/{slug}/messages 全部聊天消息
  GET /api/sessions/{slug}/plan    返回 ``<workspace_root>/sessions/<slug>/plan.md`` 原文。
                                   渲染由前端做。文件不存在返回 404 / ``error=no_plan``。
  GET /api/sessions/{slug}/plan-review
                                   返回 ``plan_review.md`` 原文（Reviewer 对 plan 的审查意见）。
                                   未启用 Reviewer / 该阶段未产出时返回 404 / ``error=no_plan_review``。
  GET /api/sessions/{slug}/code-review
                                   返回 ``code_review.md`` 原文（Reviewer 对编码的审查意见）。
                                   未启用 Reviewer / 该阶段未产出时返回 404 / ``error=no_code_review``。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from ..config.schema import HttpConfig
from ..core.state import is_open
from ..storage.db import Database

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


def _json_response(payload: Any, status: int = 200) -> web.Response:
    return web.Response(
        body=json.dumps(payload, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json",
    )


def _parse_event_payload(row: dict[str, Any]) -> dict[str, Any]:
    """events 表 payload_json 是字符串；前端方便起见解析回对象。"""
    out = dict(row)
    raw = out.get("payload_json")
    if isinstance(raw, str) and raw:
        try:
            out["payload"] = json.loads(raw)
        except json.JSONDecodeError:
            out["payload"] = raw
    else:
        out["payload"] = None
    out.pop("payload_json", None)
    return out


class WebServer:
    def __init__(self, db: Database, cfg: HttpConfig, workspace_root: Path) -> None:
        self.db = db
        self.cfg = cfg
        self.workspace_root = workspace_root.expanduser()
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/sessions", self._handle_list)
        app.router.add_get("/api/sessions/{slug}", self._handle_detail)
        app.router.add_get("/api/sessions/{slug}/events", self._handle_events)
        app.router.add_get("/api/sessions/{slug}/messages", self._handle_messages)
        app.router.add_get("/api/sessions/{slug}/plan", self._handle_plan)
        app.router.add_get("/api/sessions/{slug}/plan-review", self._handle_plan_review)
        app.router.add_get("/api/sessions/{slug}/code-review", self._handle_code_review)
        return app

    async def start(self) -> None:
        if not self.cfg.enabled:
            logger.info("HTTP 面板已禁用（http.enabled=false）")
            return
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.cfg.bind, self.cfg.port)
        await self._site.start()
        logger.info("HTTP 面板已启动：http://%s:%d/", self.cfg.bind, self.cfg.port)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---------- handlers ----------

    async def _handle_index(self, _request: web.Request) -> web.StreamResponse:
        index = _STATIC_DIR / "index.html"
        if not index.exists():
            return web.Response(text="index.html 缺失", status=500)
        return web.FileResponse(index)

    async def _handle_list(self, request: web.Request) -> web.Response:
        state_filter = request.query.get("state")
        rows = await self.db.list_sessions()
        if state_filter == "open":
            rows = [r for r in rows if is_open(r["state"])]
        elif state_filter:
            rows = [r for r in rows if r["state"] == state_filter]
        return _json_response({"sessions": rows})

    async def _handle_detail(self, request: web.Request) -> web.Response:
        slug = request.match_info["slug"]
        row = await self.db.get_session(slug)
        if row is None:
            # 同时支持 display_slug 查询，便于前端从 /api/sessions 拿到的 display_slug 直接点
            row = await self.db.get_session_by_display_slug(slug)
        if row is None:
            return _json_response({"error": "not_found", "slug": slug}, status=404)
        return _json_response({"session": row})

    async def _resolve_slug(self, slug: str) -> str | None:
        row = await self.db.get_session(slug)
        if row is not None:
            return row["slug"]
        row = await self.db.get_session_by_display_slug(slug)
        return row["slug"] if row else None

    async def _handle_events(self, request: web.Request) -> web.Response:
        slug = request.match_info["slug"]
        internal = await self._resolve_slug(slug)
        if internal is None:
            return _json_response({"error": "not_found", "slug": slug}, status=404)
        try:
            limit = int(request.query.get("limit", "500"))
        except ValueError:
            limit = 500
        limit = max(1, min(limit, 5000))
        rows = await self.db.list_events(internal, limit=limit)
        return _json_response({"events": [_parse_event_payload(r) for r in rows]})

    async def _handle_messages(self, request: web.Request) -> web.Response:
        slug = request.match_info["slug"]
        internal = await self._resolve_slug(slug)
        if internal is None:
            return _json_response({"error": "not_found", "slug": slug}, status=404)
        rows = await self.db.list_messages(internal)
        return _json_response({"messages": rows})

    async def _serve_session_md(
        self, slug: str, filename: str, missing_error: str
    ) -> web.Response:
        """读 ``sessions/<slug>/<filename>`` 原文并返回标准响应。

        三类 markdown 文件（plan.md / plan_review.md / code_review.md）共用：
        路径防穿越、缺文件 404、读失败 500、mtime 字段格式完全一致。前端只看
        ``plan`` 字段名（与最初的 /plan 路由保持一致），不为新接口换名。
        """
        internal = await self._resolve_slug(slug)
        if internal is None:
            return _json_response({"error": "not_found", "slug": slug}, status=404)
        base = (self.workspace_root / "sessions").resolve()
        path = (base / internal / filename).resolve()
        # 兜底防御路径穿越：即使 internal 已经从 db 取出，仍校验 resolve 后仍位于 base 之内
        if not path.is_relative_to(base):
            return _json_response({"error": "bad_path", "slug": slug}, status=400)
        if not path.is_file():
            return _json_response(
                {"error": missing_error, "slug": slug, "path": str(path)}, status=404
            )
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return _json_response(
                {"error": "read_failed", "slug": slug, "detail": str(exc)}, status=500
            )
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        return _json_response({"plan": text, "path": str(path), "mtime": mtime})

    async def _handle_plan(self, request: web.Request) -> web.Response:
        return await self._serve_session_md(
            request.match_info["slug"], "plan.md", "no_plan"
        )

    async def _handle_plan_review(self, request: web.Request) -> web.Response:
        return await self._serve_session_md(
            request.match_info["slug"], "plan_review.md", "no_plan_review"
        )

    async def _handle_code_review(self, request: web.Request) -> web.Response:
        return await self._serve_session_md(
            request.match_info["slug"], "code_review.md", "no_code_review"
        )
