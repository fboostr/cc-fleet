"""共享子进程引擎 run_subprocess 的验证：

- 纯判定 ``_overrun`` 的三档边界（穷举，不起子进程，快）。
- ``_ActivityTracker`` 的「最近活动 + 工具在飞」跟踪（可注入时钟）。
- run_subprocess 真子进程集成：happy path、空闲超时回收、kill_event 强杀。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from cc_fleet.core.runners.base import TimeoutPolicy
from cc_fleet.core.runners.engine import _ActivityTracker, _overrun, run_subprocess


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


class _ToolInterp(_FakeInterp):
    """带工具生命周期识别的 interpreter：evt['tool'] = ('start'|'end', id)。"""

    def tool_activity(self, evt: dict) -> list[tuple[str, str]]:
        t = evt.get("tool")
        return [t] if isinstance(t, tuple) else []


# ── 纯判定 _overrun 的三档 ─────────────────────────────────────────────
# 阈值：idle=10, tool=100, hard_cap=1000。now/start/last_event_at 用相对数即可。
_POLICY = TimeoutPolicy(idle_sec=10, tool_sec=100, hard_cap_sec=1000)


def test_overrun_none_when_within_all_tiers():
    # 静默 5s（< idle 10），总时长 5s（< hard_cap）→ 不超时
    assert (
        _overrun(now=5, start=0, last_event_at=0, in_flight=False, timeout=_POLICY)
        is None
    )


def test_overrun_idle_triggers_when_no_tool_in_flight():
    # 无工具在飞、静默 11s > idle 10 → idle 档
    assert (
        _overrun(now=11, start=0, last_event_at=0, in_flight=False, timeout=_POLICY)
        == "idle"
    )


def test_overrun_tool_in_flight_suppresses_idle_uses_tool_tier():
    # 有工具在飞：静默 50s 早过 idle(10) 但 < tool(100) → 不误杀（正是长编译/测试场景）
    assert (
        _overrun(now=50, start=0, last_event_at=0, in_flight=True, timeout=_POLICY)
        is None
    )
    # 静默 101s > tool(100) → tool 档
    assert (
        _overrun(now=101, start=0, last_event_at=0, in_flight=True, timeout=_POLICY)
        == "tool"
    )


def test_overrun_hard_cap_has_priority_even_while_tool_in_flight():
    # 总时长 1001s > hard_cap(1000)，即便有工具在飞、静默为 0 → hard_cap 优先兜底
    assert (
        _overrun(
            now=1001, start=0, last_event_at=1001, in_flight=True, timeout=_POLICY
        )
        == "hard_cap"
    )


# ── _ActivityTracker：最近活动 + 工具在飞 ──────────────────────────────
def test_activity_tracker_refreshes_last_event_at():
    clk = {"t": 100.0}
    tr = _ActivityTracker(lambda: clk["t"])
    assert tr.last_event_at == 100.0
    clk["t"] = 250.0
    tr.note_event({"text": "hi"}, _FakeInterp())
    assert tr.last_event_at == 250.0


def test_activity_tracker_tool_in_flight_start_then_end():
    clk = {"t": 0.0}
    tr = _ActivityTracker(lambda: clk["t"])
    interp = _ToolInterp()
    assert tr.tool_in_flight is False
    tr.note_event({"tool": ("start", "t1")}, interp)
    assert tr.tool_in_flight is True
    # 另一个工具并行开始，仍在飞
    tr.note_event({"tool": ("start", "t2")}, interp)
    assert tr.tool_in_flight is True
    tr.note_event({"tool": ("end", "t1")}, interp)
    assert tr.tool_in_flight is True  # t2 未回
    tr.note_event({"tool": ("end", "t2")}, interp)
    assert tr.tool_in_flight is False


def test_activity_tracker_without_tool_activity_stays_not_in_flight():
    # interpreter 未实现 tool_activity → 退化为「只更 last_event_at、恒无工具在飞」
    tr = _ActivityTracker(lambda: 0.0)
    tr.note_event({"text": "x"}, _FakeInterp())
    assert tr.tool_in_flight is False


# ── run_subprocess 真子进程集成 ────────────────────────────────────────
async def test_run_subprocess_happy_path(tmp_path: Path):
    """子进程吐一行 JSON → 引擎按注入的 interpreter 抽文本 / sid，并把流原样落盘。"""
    log = tmp_path / "stream.jsonl"
    argv = ["sh", "-c", """printf '%s\\n' '{"text":"hello","sid":"s1"}'"""]
    res = await run_subprocess(
        argv=argv,
        cwd=tmp_path,
        stdin_text="ignored",
        env=os.environ.copy(),
        timeout=TimeoutPolicy(10, 10, 10),
        stream_log_path=log,
        interpreter=_FakeInterp(),
    )
    assert res.exit_code == 0
    assert res.timed_out is False
    assert res.killed is False
    assert res.timeout_kind is None
    assert res.text_output == "hello"
    assert res.init_session_id == "s1"
    assert len(res.events) == 1
    assert "hello" in log.read_text(encoding="utf-8")


async def test_run_subprocess_idle_timeout_sets_kind(tmp_path: Path):
    """无事件、无工具在飞的子进程睡过 idle_sec → 空闲档回收、timeout_kind='idle'。

    hard_cap 拉大（30s）确保先命中空闲档而非绝对上限；实测引擎超时回收路径。
    """
    argv = ["sh", "-c", "sleep 30"]
    res = await run_subprocess(
        argv=argv,
        cwd=tmp_path,
        stdin_text="",
        env=os.environ.copy(),
        timeout=TimeoutPolicy(idle_sec=1, tool_sec=30, hard_cap_sec=30),
        stream_log_path=tmp_path / "s.jsonl",
        interpreter=_FakeInterp(),
    )
    assert res.timed_out is True
    assert res.timeout_kind == "idle"
    assert res.killed is False


async def test_run_subprocess_kill_event_hard_kills(tmp_path: Path):
    """kill_event 被 set → 引擎立即杀进程组、killed=True 且非超时（/kill 强杀路径）。"""
    kill = asyncio.Event()
    kill.set()  # 预置：监控循环首个轮询即响应，杀掉长睡子进程
    argv = ["sh", "-c", "sleep 30"]
    res = await run_subprocess(
        argv=argv,
        cwd=tmp_path,
        stdin_text="",
        env=os.environ.copy(),
        timeout=TimeoutPolicy(idle_sec=60, tool_sec=60, hard_cap_sec=60),
        stream_log_path=tmp_path / "s.jsonl",
        interpreter=_FakeInterp(),
        kill_event=kill,
    )
    assert res.killed is True
    assert res.timed_out is False
    assert res.timeout_kind is None
