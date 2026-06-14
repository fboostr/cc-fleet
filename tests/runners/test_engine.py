"""共享子进程引擎 run_subprocess 的真子进程验证 + StreamInterpreter 注入（P1 新增）。"""

from __future__ import annotations

import os
from pathlib import Path


from cc_fleet.core.runners.engine import run_subprocess


class _FakeInterp:
    """最小 StreamInterpreter：从 evt 的 text 字段抽文本、sid 字段抽会话 id。"""

    def consume(self, evt: dict, parts: list[str]) -> None:
        if isinstance(evt.get("text"), str):
            parts.append(evt["text"])

    def session_id(self, evt: dict) -> str | None:
        sid = evt.get("sid")
        return sid if isinstance(sid, str) else None

    def terminal_error(self, events: list[dict]) -> tuple[bool, str | None]:
        return False, None


async def test_run_subprocess_happy_path(tmp_path: Path):
    """子进程吐一行 JSON → 引擎按注入的 interpreter 抽文本 / sid，并把流原样落盘。"""
    log = tmp_path / "stream.jsonl"
    argv = ["sh", "-c", """printf '%s\\n' '{"text":"hello","sid":"s1"}'"""]
    res = await run_subprocess(
        argv=argv,
        cwd=tmp_path,
        stdin_text="ignored",
        env=os.environ.copy(),
        timeout_sec=10,
        stream_log_path=log,
        interpreter=_FakeInterp(),
    )
    assert res.exit_code == 0
    assert res.timed_out is False
    assert res.text_output == "hello"
    assert res.init_session_id == "s1"
    assert len(res.events) == 1
    assert "hello" in log.read_text(encoding="utf-8")


async def test_run_subprocess_timeout_sets_flag(tmp_path: Path):
    """子进程睡过超时 → SIGTERM 杀进程组、timed_out=True（实测引擎超时回收路径）。"""
    argv = ["sh", "-c", "sleep 30"]
    res = await run_subprocess(
        argv=argv,
        cwd=tmp_path,
        stdin_text="",
        env=os.environ.copy(),
        timeout_sec=1,
        stream_log_path=tmp_path / "s.jsonl",
        interpreter=_FakeInterp(),
    )
    assert res.timed_out is True
