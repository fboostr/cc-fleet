"""codex runner：argv 拼装、JSONL 事件解析、CodexRunner 归一参数映射（PR-B 新增）。

失败路径 fixture 取自 codex-cli 0.142.5 未登录状态的一次**真跑**（`codex exec --json`，
401 重连后 turn.failed），字段名可信；成功路径（agent_message / turn.completed /
item.started）按官方 JSONL 文档形态构造，待登录后一次真跑复核。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_fleet.config.schema import CodexConfig
from cc_fleet.core.runners import codex as codex_mod
from cc_fleet.core.runners.base import AgentPermission, GuardrailHandle, TimeoutPolicy
from cc_fleet.core.runners.codex import (
    CodexGuardrailProvider,
    CodexInterpreter,
    CodexRunner,
    build_codex_args,
)
from cc_fleet.core.runners.engine import EngineResult

# ---- 真跑抓取（未登录 401 失败路径，字段名实测可信）----
REAL_THREAD_STARTED = {
    "type": "thread.started",
    "thread_id": "019f59b8-3a97-7b13-907b-3414a0ba1595",
}
REAL_TRANSIENT_ERROR = {
    "type": "error",
    "message": "Reconnecting... 2/5 (unexpected status 401 Unauthorized ...)",
}
REAL_ITEM_ERROR = {
    "type": "item.completed",
    "item": {"id": "item_0", "type": "error", "message": "Falling back from WebSockets ..."},
}
REAL_TURN_FAILED = {
    "type": "turn.failed",
    "error": {"message": "unexpected status 401 Unauthorized: Missing bearer ..."},
}

# ---- 成功路径（按官方 JSONL 文档形态构造，待真跑复核）----
DOC_TURN_STARTED = {"type": "turn.started"}
DOC_CMD_STARTED = {
    "type": "item.started",
    "item": {"id": "item_1", "type": "command_execution", "command": "pytest -q"},
}
DOC_CMD_COMPLETED = {
    "type": "item.completed",
    "item": {"id": "item_1", "type": "command_execution", "exit_code": 0},
}
DOC_MSG_COMPLETED = {
    "type": "item.completed",
    "item": {"id": "item_2", "type": "agent_message", "text": "开发完成。\n\nSLUG: x\n"},
}
DOC_TURN_COMPLETED = {"type": "turn.completed", "usage": {"input_tokens": 1}}


# ---- build_codex_args ----

def test_build_args_fresh_read_only(tmp_path: Path):
    args = build_codex_args(
        binary="codex", sandbox_mode="read-only", resume_from=None,
        last_message_path=tmp_path / "last.txt", model=None,
    )
    assert args[:2] == ["codex", "exec"]
    assert ["--sandbox", "read-only"] == args[2:4]
    assert "--json" in args
    assert ["--output-last-message", str(tmp_path / "last.txt")] == args[5:7]
    assert "resume" not in args and "--model" not in args
    assert args[-1] == "-"  # prompt 经 stdin


def test_build_args_resume_uses_subcommand_and_config_override(tmp_path: Path):
    """resume 子命令没有 --sandbox flag，档位经 -c sandbox_mode= 传入（dev 写档提权靠它）。"""
    args = build_codex_args(
        binary="codex", sandbox_mode="workspace-write", resume_from="SID-1",
        last_message_path=tmp_path / "last.txt", model="gpt-5.3-codex",
    )
    assert args[:4] == ["codex", "exec", "resume", "SID-1"]
    assert ["-c", 'sandbox_mode="workspace-write"'] == args[4:6]
    assert "--sandbox" not in args
    assert ["--model", "gpt-5.3-codex"] == args[-3:-1]
    assert args[-1] == "-"


# ---- CodexInterpreter ----

def test_interpreter_captures_thread_id_and_terminal_failure():
    interp = CodexInterpreter()
    events = [REAL_THREAD_STARTED, DOC_TURN_STARTED, REAL_TRANSIENT_ERROR,
              REAL_ITEM_ERROR, REAL_TURN_FAILED]
    assert interp.session_id(REAL_THREAD_STARTED) == "019f59b8-3a97-7b13-907b-3414a0ba1595"
    assert interp.session_id(DOC_TURN_STARTED) is None
    is_err, msg = interp.terminal_error(events)
    assert is_err is True
    assert "401" in (msg or "")


def test_interpreter_transient_error_not_terminal_when_turn_completed():
    """关键回归：瞬态 error 事件（网络重连）夹在成功轮里，不得误判为终态失败。"""
    interp = CodexInterpreter()
    events = [REAL_THREAD_STARTED, DOC_TURN_STARTED, REAL_TRANSIENT_ERROR,
              DOC_MSG_COMPLETED, DOC_TURN_COMPLETED]
    assert interp.terminal_error(events) == (False, None)


def test_interpreter_no_turn_terminal_returns_false():
    """流中途被杀（无 turn.completed/failed）：不判终态错误，交给 exit_code/超时路径。"""
    interp = CodexInterpreter()
    assert CodexInterpreter().terminal_error([REAL_THREAD_STARTED, REAL_TRANSIENT_ERROR]) == (False, None)
    assert interp.terminal_error([]) == (False, None)


def test_interpreter_extracts_agent_message_text_only():
    interp = CodexInterpreter()
    parts: list[str] = []
    for evt in (REAL_THREAD_STARTED, DOC_CMD_STARTED, DOC_CMD_COMPLETED,
                DOC_MSG_COMPLETED, REAL_ITEM_ERROR, DOC_TURN_COMPLETED):
        interp.consume(evt, parts)
    assert parts == ["开发完成。\n\nSLUG: x\n"]  # 只收 agent_message，error/command 项不收


def test_interpreter_tool_activity_pairs_item_lifecycle():
    interp = CodexInterpreter()
    assert interp.tool_activity(DOC_CMD_STARTED) == [("start", "item_1")]
    assert interp.tool_activity(DOC_CMD_COMPLETED) == [("end", "item_1")]
    assert interp.tool_activity(DOC_TURN_COMPLETED) == []
    assert interp.tool_activity(REAL_TRANSIENT_ERROR) == []


# ---- CodexGuardrailProvider ----

def test_guardrail_provider_returns_empty_handle(tmp_path: Path):
    handle = CodexGuardrailProvider().prepare(settings_dir=tmp_path)
    assert handle.settings_path is None
    assert handle.extra_cli_args == [] and handle.env == {}


# ---- CodexRunner.run 归一参数映射 ----

def _policy() -> TimeoutPolicy:
    return TimeoutPolicy(idle_sec=5, tool_sec=5, hard_cap_sec=5)


async def _run(monkeypatch, tmp_path: Path, *, permission, resume_from=None,
               protocol_text="", events=None, last_message: str | None = None,
               model: str | None = None):
    """跑一次 CodexRunner.run，返回 (captured_kwargs, result)。"""
    captured: dict = {}

    async def fake_run_subprocess(**kwargs):
        captured.update(kwargs)
        if last_message is not None:
            # 模拟 codex 把最终回复写进 --output-last-message 指定的文件
            lm = kwargs["argv"][kwargs["argv"].index("--output-last-message") + 1]
            Path(lm).write_text(last_message, encoding="utf-8")
        return EngineResult(
            exit_code=0, text_output="流内文本", init_session_id="captured-tid",
            stderr_tail="", timed_out=False, events=events or [],
            result_is_error=False, error_message=None,
        )

    monkeypatch.setattr(codex_mod, "run_subprocess", fake_run_subprocess)
    runner = CodexRunner(CodexConfig(model=model))
    result = await runner.run(
        prompt="按 plan 完成开发",
        cwd=tmp_path,
        permission=permission,
        protocol_text=protocol_text,
        session_id="ignored-preassigned",
        resume_from=resume_from,
        guardrail=GuardrailHandle(settings_path=None, extra_cli_args=[], env={"X_G": "1"}),
        timeout=_policy(),
        stream_log_path=tmp_path / "stream.jsonl",
        extra_env={"X_E": "2"},
        on_event=None,
    )
    return captured, result


async def test_runner_maps_permission_to_sandbox(monkeypatch, tmp_path):
    captured, _ = await _run(monkeypatch, tmp_path, permission=AgentPermission.READ_ONLY)
    assert ["--sandbox", "read-only"] == captured["argv"][2:4]
    captured, _ = await _run(monkeypatch, tmp_path, permission=AgentPermission.WRITE)
    assert ["--sandbox", "workspace-write"] == captured["argv"][2:4]


async def test_runner_resume_and_env_merge(monkeypatch, tmp_path):
    captured, result = await _run(
        monkeypatch, tmp_path, permission=AgentPermission.WRITE, resume_from="tid-old",
    )
    assert captured["argv"][2:4] == ["resume", "tid-old"]
    assert captured["env"]["X_G"] == "1" and captured["env"]["X_E"] == "2"
    assert result.session_id == "captured-tid"  # 捕获优先于 resume_from


async def test_runner_prepends_protocol_and_write_guard_clause(monkeypatch, tmp_path):
    captured, _ = await _run(
        monkeypatch, tmp_path, permission=AgentPermission.WRITE, protocol_text="## 协议\n规则",
    )
    stdin = captured["stdin_text"]
    # 顺序：codex 纪律条款 → 协议文本 → 用户 prompt
    assert stdin.index("安全纪律") < stdin.index("## 协议") < stdin.index("按 plan 完成开发")
    # 只读阶段不注入纪律条款
    captured, _ = await _run(
        monkeypatch, tmp_path, permission=AgentPermission.READ_ONLY, protocol_text="## 协议\n规则",
    )
    assert "安全纪律" not in captured["stdin_text"]
    assert captured["stdin_text"].startswith("## 协议")


async def test_runner_prefers_last_message_file_over_stream_text(monkeypatch, tmp_path):
    _, result = await _run(
        monkeypatch, tmp_path, permission=AgentPermission.READ_ONLY,
        last_message="权威最终回复\nSLUG: y\n",
    )
    assert result.text_output == "权威最终回复\nSLUG: y\n"
    # 文件缺失 / 为空时回退流内拼接
    _, result = await _run(monkeypatch, tmp_path, permission=AgentPermission.READ_ONLY)
    assert result.text_output == "流内文本"


async def test_runner_model_flag(monkeypatch, tmp_path):
    captured, _ = await _run(
        monkeypatch, tmp_path, permission=AgentPermission.READ_ONLY, model="gpt-5.3-codex",
    )
    assert "--model" in captured["argv"] and "gpt-5.3-codex" in captured["argv"]
