"""opencode runner：argv 拼装、JSON 事件解析、OpencodeRunner 归一参数映射（PR-C 新增）。

fixture 均取自 opencode 1.17.15 的**真跑**抓取（`opencode run --format json`，含成功、
工具调用、错误三条路径），字段名实测可信；id 类值已截短。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_fleet.config.schema import OpencodeConfig
from cc_fleet.core.runners import opencode as oc_mod
from cc_fleet.core.runners.base import AgentPermission, GuardrailHandle, TimeoutPolicy
from cc_fleet.core.runners.engine import EngineResult
from cc_fleet.core.runners.opencode import (
    OpencodeGuardrailProvider,
    OpencodeInterpreter,
    OpencodeRunner,
    build_opencode_args,
)

# ---- 真跑抓取（opencode 1.17.15）----
SID = "ses_0a635d7d4ffeMxp6P5YMm6GoI3"  # 注意大小写混合：依赖 sid 正则放宽
STEP_START = {
    "type": "step_start", "timestamp": 1, "sessionID": SID,
    "part": {"id": "prt_a1", "messageID": "msg_m1", "sessionID": SID, "type": "step-start"},
}
TEXT_EVT = {
    "type": "text", "timestamp": 2, "sessionID": SID,
    "part": {"id": "prt_t1", "messageID": "msg_m1", "sessionID": SID,
             "type": "text", "text": "ok", "time": {"start": 2, "end": 3}},
}
STEP_FINISH = {
    "type": "step_finish", "timestamp": 4, "sessionID": SID,
    "part": {"id": "prt_f1", "reason": "stop", "messageID": "msg_m1", "sessionID": SID,
             "type": "step-finish", "tokens": {"total": 10}},
}
TOOL_USE = {  # 工具调用只在结束时发一条（运行期间流静默）
    "type": "tool_use", "timestamp": 5, "sessionID": SID,
    "part": {"id": "prt_tool1", "messageID": "msg_m1", "sessionID": SID, "type": "tool",
             "tool": "bash", "callID": "call_x", "state": {"status": "completed"}},
}
ERROR_EVT = {
    "type": "error", "timestamp": 6, "sessionID": SID,
    "error": {"name": "UnknownError",
              "data": {"message": "Unexpected server error. Check server logs for details.",
                       "ref": "err_395bd21b"}},
}


# ---- build_opencode_args ----

def test_build_args_read_only_uses_plan_agent():
    args = build_opencode_args(
        binary="opencode", permission=AgentPermission.READ_ONLY, resume_from=None, model=None,
    )
    assert args == ["opencode", "run", "--format", "json", "--agent", "plan"]


def test_build_args_write_uses_build_agent_with_auto():
    """1.17.15 无 --dangerously-skip-permissions，非交互放行权限靠 --auto。"""
    args = build_opencode_args(
        binary="opencode", permission=AgentPermission.WRITE, resume_from=None, model=None,
    )
    assert args == ["opencode", "run", "--format", "json", "--agent", "build", "--auto"]


def test_build_args_resume_and_model():
    args = build_opencode_args(
        binary="/x/opencode", permission=AgentPermission.WRITE,
        resume_from=SID, model="anthropic/claude-sonnet-5",
    )
    assert args[:4] == ["/x/opencode", "run", "--format", "json"]
    assert ["--session", SID] == args[4:6]
    assert ["--model", "anthropic/claude-sonnet-5"] == args[-2:]


# ---- OpencodeInterpreter ----

def test_interpreter_captures_session_id_from_any_event():
    interp = OpencodeInterpreter()
    assert interp.session_id(STEP_START) == SID
    assert interp.session_id(TEXT_EVT) == SID
    assert interp.session_id({"type": "x"}) is None


def test_interpreter_extracts_text_and_dedupes_by_part_id():
    interp = OpencodeInterpreter()
    parts: list[str] = []
    interp.consume(STEP_START, parts)
    interp.consume(TEXT_EVT, parts)
    interp.consume(TOOL_USE, parts)
    assert parts == ["ok"]
    # 同 part 重发（防御上游改为增量形态）：原地覆盖而非重复追加
    updated = {**TEXT_EVT, "part": {**TEXT_EVT["part"], "text": "ok final"}}
    interp.consume(updated, parts)
    assert parts == ["ok final"]
    # 新 part 正常追加
    other = {**TEXT_EVT, "part": {**TEXT_EVT["part"], "id": "prt_t2", "text": "tail"}}
    interp.consume(other, parts)
    assert parts == ["ok final", "tail"]


def test_interpreter_error_event_is_terminal():
    interp = OpencodeInterpreter()
    is_err, msg = interp.terminal_error([STEP_START, ERROR_EVT])
    assert is_err is True
    assert "Unexpected server error" in (msg or "")


def test_interpreter_error_before_recovery_not_terminal():
    """error 之后仍有正常推进（step_finish/text）：内部已恢复，不判终态失败。"""
    interp = OpencodeInterpreter()
    assert interp.terminal_error([STEP_START, ERROR_EVT, TEXT_EVT, STEP_FINISH]) == (False, None)


def test_interpreter_tool_failure_not_terminal():
    """tool_use 的 state.status=error 是工具级失败，模型会自行处理，不是终态。"""
    interp = OpencodeInterpreter()
    failed_tool = {**TOOL_USE, "part": {**TOOL_USE["part"], "state": {"status": "error"}}}
    assert interp.terminal_error([STEP_START, failed_tool, TEXT_EVT, STEP_FINISH]) == (False, None)
    assert interp.terminal_error([]) == (False, None)


def test_interpreter_step_window_is_tool_activity():
    """工具运行期间流静默（tool_use 只在结束发一条），in-flight 用 step 窗口按 messageID 配对。"""
    interp = OpencodeInterpreter()
    assert interp.tool_activity(STEP_START) == [("start", "msg_m1")]
    assert interp.tool_activity(STEP_FINISH) == [("end", "msg_m1")]
    assert interp.tool_activity(TOOL_USE) == []
    assert interp.tool_activity(TEXT_EVT) == []


# ---- OpencodeGuardrailProvider ----

def test_guardrail_provider_returns_empty_handle(tmp_path: Path):
    handle = OpencodeGuardrailProvider().prepare(settings_dir=tmp_path)
    assert handle.settings_path is None
    assert handle.extra_cli_args == [] and handle.env == {}


# ---- OpencodeRunner.run 归一参数映射 ----

async def _run(monkeypatch, tmp_path: Path, *, permission, resume_from=None, protocol_text=""):
    captured: dict = {}

    async def fake_run_subprocess(**kwargs):
        captured.update(kwargs)
        return EngineResult(
            exit_code=0, text_output="流内文本", init_session_id=SID,
            stderr_tail="", timed_out=False, events=[],
            result_is_error=False, error_message=None,
        )

    monkeypatch.setattr(oc_mod, "run_subprocess", fake_run_subprocess)
    runner = OpencodeRunner(OpencodeConfig())
    result = await runner.run(
        prompt="按 plan 完成开发",
        cwd=tmp_path,
        permission=permission,
        protocol_text=protocol_text,
        session_id="ignored",
        resume_from=resume_from,
        guardrail=GuardrailHandle(settings_path=None, extra_cli_args=[], env={"X_G": "1"}),
        timeout=TimeoutPolicy(idle_sec=5, tool_sec=5, hard_cap_sec=5),
        stream_log_path=tmp_path / "stream.jsonl",
        extra_env={"X_E": "2"},
        on_event=None,
    )
    return captured, result


async def test_runner_maps_permission_to_agent(monkeypatch, tmp_path):
    captured, _ = await _run(monkeypatch, tmp_path, permission=AgentPermission.READ_ONLY)
    assert ["--agent", "plan"] == captured["argv"][-2:]
    assert "--auto" not in captured["argv"]
    captured, _ = await _run(monkeypatch, tmp_path, permission=AgentPermission.WRITE)
    assert ["--agent", "build", "--auto"] == captured["argv"][-3:]


async def test_runner_captures_session_id_and_merges_env(monkeypatch, tmp_path):
    captured, result = await _run(
        monkeypatch, tmp_path, permission=AgentPermission.READ_ONLY, resume_from="ses_old",
    )
    assert ["--session", "ses_old"] == captured["argv"][4:6]
    assert result.session_id == SID  # 捕获优先于 resume_from
    assert captured["env"]["X_G"] == "1" and captured["env"]["X_E"] == "2"
    assert result.text_output == "流内文本"  # 无 last-message 文件，流内拼接即最终文本


async def test_runner_prepends_protocol_and_write_guard_clause(monkeypatch, tmp_path):
    captured, _ = await _run(
        monkeypatch, tmp_path, permission=AgentPermission.WRITE, protocol_text="## 协议\n规则",
    )
    stdin = captured["stdin_text"]
    assert stdin.index("安全纪律") < stdin.index("## 协议") < stdin.index("按 plan 完成开发")
    captured, _ = await _run(
        monkeypatch, tmp_path, permission=AgentPermission.READ_ONLY, protocol_text="## 协议\n规则",
    )
    assert "安全纪律" not in captured["stdin_text"]
    assert captured["stdin_text"].startswith("## 协议")
