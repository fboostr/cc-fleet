"""验证 build_claude_args 的关键分支，以及失败上报抽取 / 拼装。"""

from __future__ import annotations

import errno
import os
import signal
from pathlib import Path

import pytest

from cc_fleet.core.runners.base import ClaudeRunResult
from cc_fleet.core.runners.claude import (
    LENGTH_ERROR_HINT,
    _terminal_result_error,
    build_claude_args,
    classify_length_error,
    format_run_failure,
)
from cc_fleet.core.runners.engine import _escape_leading_slash, _terminate_process_tree


def _result(**overrides) -> ClaudeRunResult:
    """构造一个最小 ClaudeRunResult，仅覆盖关心的字段。"""
    base = dict(
        exit_code=0,
        session_id="sid",
        text_output="",
        stream_log_path=Path("/tmp/stream.jsonl"),
        stderr_tail="",
        timed_out=False,
    )
    base.update(overrides)
    return ClaudeRunResult(**base)


def test_default_args_use_session_id_not_resume(tmp_path: Path):
    args = build_claude_args(
        binary="claude",
        session_id="00000000-0000-4000-8000-000000000001",
        permission_mode="plan",
        resume_from=None,
        settings_path=None,
        append_system_prompt_file=None,
    )
    assert args[0] == "claude"
    assert "--permission-mode" in args and "plan" in args
    assert "--output-format" in args and "stream-json" in args
    assert "--dangerously-skip-permissions" in args
    assert "--session-id" in args
    assert "--resume" not in args


def test_resume_mode_drops_session_id():
    args = build_claude_args(
        binary="claude",
        session_id="should-be-ignored",
        permission_mode="acceptEdits",
        resume_from="abc-123",
        settings_path=None,
        append_system_prompt_file=None,
    )
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in args


def test_settings_and_system_prompt_file(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    sysp = tmp_path / "sys.md"
    sysp.write_text("plan 协议要求...", encoding="utf-8")
    args = build_claude_args(
        binary="/usr/local/bin/claude",
        session_id="11111111-1111-4111-8111-111111111111",
        permission_mode="plan",
        resume_from=None,
        settings_path=settings,
        append_system_prompt_file=sysp,
    )
    assert "--settings" in args
    assert args[args.index("--settings") + 1] == str(settings)
    assert "--append-system-prompt" in args
    assert args[args.index("--append-system-prompt") + 1] == "plan 协议要求..."


def test_empty_system_prompt_file_skipped(tmp_path: Path):
    sysp = tmp_path / "empty.md"
    sysp.write_text("", encoding="utf-8")
    args = build_claude_args(
        binary="claude",
        session_id="22222222-2222-4222-8222-222222222222",
        permission_mode="plan",
        resume_from=None,
        settings_path=None,
        append_system_prompt_file=sysp,
    )
    assert "--append-system-prompt" not in args


def test_prompt_starting_with_slash_is_escaped():
    """回归 req-20260519-132428-f5ff：用户首条消息以 `/` 起头时，CLI 会当 slash
    指令解析（返回 Unknown command）。prompt 改走 stdin 后，ZWSP 兜底仍由
    `_escape_leading_slash` 在写 stdin 前施加。"""
    forwarded = _escape_leading_slash("/list 返回的表格，有两处需要修改")
    # 首字符不应再是 `/`；ZWSP 前缀必须存在且保留原文
    assert not forwarded.startswith("/")
    assert forwarded == "​/list 返回的表格，有两处需要修改"


def test_prompt_not_starting_with_slash_unchanged():
    """非 `/` 起头的正常 prompt 不应被改写，避免 ZWSP 误伤。"""
    assert _escape_leading_slash("正常需求文本") == "正常需求文本"


def test_build_args_no_positional_prompt():
    """prompt 改走 stdin 后，argv 里 `-p` 之后不应再跟 prompt 位置参数。"""
    args = build_claude_args(
        binary="claude",
        session_id="55555555-5555-4555-8555-555555555555",
        permission_mode="plan",
        resume_from=None,
        settings_path=None,
        append_system_prompt_file=None,
    )
    # `-p` 紧跟的应是下一个 flag（--permission-mode），而非 prompt 文本
    assert args[args.index("-p") + 1] == "--permission-mode"


# ---- 失败上报：从 result 事件抽取 ----


def test_terminal_result_error_extracts_message():
    """回归 req-20260529-191122-9409：终态 result 事件 is_error=true 时抽出真实错误文本。"""
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "The model's tool call could not be parsed (retry also failed).",
        },
    ]
    is_error, msg = _terminal_result_error(events)
    assert is_error is True
    assert msg == "The model's tool call could not be parsed (retry also failed)."


def test_terminal_result_error_success_returns_none():
    events = [{"type": "result", "subtype": "success", "is_error": False, "result": "done"}]
    assert _terminal_result_error(events) == (False, None)


def test_terminal_result_error_no_result_event():
    events = [{"type": "assistant", "message": {"content": []}}]
    assert _terminal_result_error(events) == (False, None)


def test_terminal_result_error_is_error_without_text():
    """is_error 但无人读文本：标记仍为 True，文本为 None（由上报回退 stderr）。"""
    events = [{"type": "result", "is_error": True, "result": ""}]
    assert _terminal_result_error(events) == (True, None)


# ---- 失败上报：拼装文本 ----


def test_format_run_failure_prefers_result_message():
    r = _result(exit_code=1, stderr_tail="", result_is_error=True, error_message="boom from result")
    text = format_run_failure(r, "plan")
    assert "plan 阶段失败" in text
    assert "exit=1" in text
    assert "boom from result" in text


def test_format_run_failure_falls_back_to_stderr():
    r = _result(exit_code=1, stderr_tail="ssh: Could not resolve hostname dev01.example")
    text = format_run_failure(r, "dev")
    assert "dev 阶段失败：exit=1" in text
    assert "ssh: Could not resolve hostname dev01.example" in text


def test_format_run_failure_exit_zero_but_is_error():
    """exit=0 但模型层报错：不带 exit= 段，但带真实错误文本。"""
    r = _result(exit_code=0, result_is_error=True, error_message="model error")
    text = format_run_failure(r, "plan")
    assert "exit=" not in text
    assert "plan 阶段失败" in text
    assert "model error" in text


# ---- 长度 / 上下文过长的明确提示 ----


def test_classify_length_error_oserror_e2big():
    """OSError E2BIG（命令行参数过长）应被识别（stdin 改造后的兜底分支）。"""
    reason = classify_length_error(OSError(errno.E2BIG, "Argument list too long"))
    assert reason is not None
    assert "命令行参数过长" in reason


def test_classify_length_error_oserror_other_is_none():
    """非 E2BIG 的 OSError 不应被误判为长度类。"""
    assert classify_length_error(OSError(errno.ENOENT, "No such file")) is None


def test_classify_length_error_model_prompt_too_long():
    """模型层「prompt is too long」应被识别。"""
    r = _result(
        result_is_error=True,
        error_message="prompt is too long: 215000 tokens > 200000 maximum",
    )
    reason = classify_length_error(r)
    assert reason is not None
    assert "上下文" in reason


def test_classify_length_error_result_not_error_is_none():
    """result_is_error=False 时即便文本含关键字也不判定（非失败态）。"""
    r = _result(result_is_error=False, error_message="prompt is too long")
    assert classify_length_error(r) is None


def test_classify_length_error_unrelated_message_is_none():
    """普通模型层报错不应被误判为长度类。"""
    r = _result(result_is_error=True, error_message="tool call could not be parsed")
    assert classify_length_error(r) is None


def test_format_run_failure_prepends_length_hint():
    """长度类失败：在原始错误前追加平实说明 + 处置建议。"""
    r = _result(
        exit_code=0,
        result_is_error=True,
        error_message="prompt is too long: 215000 tokens > 200000 maximum",
    )
    text = format_run_failure(r, "dev")
    assert "超出模型上下文窗口" in text
    assert LENGTH_ERROR_HINT in text
    # 原始模型报错仍保留
    assert "215000 tokens" in text


# ---- 超时回收：按进程组杀，连带回收 claude 派生的孙进程 ----


class _FakeProc:
    """最小子进程替身：记录是否被 send_signal（直接杀子进程的回退路径）。"""

    def __init__(self, pid: int = 4321, returncode: int | None = None):
        self.pid = pid
        self.returncode = returncode
        self.sent_signal: int | None = None

    def send_signal(self, sig: int) -> None:
        self.sent_signal = sig


def test_terminate_process_tree_kills_whole_group(monkeypatch: pytest.MonkeyPatch):
    """有 pgid 时优先 killpg 整个进程组，而非只杀直接子进程。"""
    calls: dict = {}
    monkeypatch.setattr(os, "getpgid", lambda pid: 9090)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.__setitem__("killpg", (pgid, sig)))
    proc = _FakeProc(pid=1234, returncode=None)
    _terminate_process_tree(proc, signal.SIGTERM)  # type: ignore[arg-type]
    assert calls["killpg"] == (9090, signal.SIGTERM)
    assert proc.sent_signal is None  # 未退回直接杀子进程


def test_terminate_process_tree_skips_when_already_exited(monkeypatch: pytest.MonkeyPatch):
    """子进程已退出（returncode 非 None）不再发信号，避免误杀回收后复用的 pid。"""
    called = {"killpg": False}
    monkeypatch.setattr(os, "getpgid", lambda pid: 1)
    monkeypatch.setattr(os, "killpg", lambda *a: called.__setitem__("killpg", True))
    proc = _FakeProc(returncode=0)
    _terminate_process_tree(proc, signal.SIGKILL)  # type: ignore[arg-type]
    assert called["killpg"] is False
    assert proc.sent_signal is None


def test_terminate_process_tree_falls_back_to_direct_kill(monkeypatch: pytest.MonkeyPatch):
    """拿不到进程组（killpg 抛 OSError）时退回只杀直接子进程，保证不抛错。"""
    monkeypatch.setattr(os, "getpgid", lambda pid: 1)

    def boom(*_a):
        raise OSError("no such process group")

    monkeypatch.setattr(os, "killpg", boom)
    proc = _FakeProc(returncode=None)
    _terminate_process_tree(proc, signal.SIGTERM)  # type: ignore[arg-type]
    assert proc.sent_signal == signal.SIGTERM


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="进程组语义仅 POSIX")
async def test_real_subprocess_group_kill_reaps_grandchild():
    """端到端：start_new_session 起的子进程派生孙进程，_terminate_process_tree 应连孙进程一起杀。"""
    import asyncio

    # 父 sh 后台派生一个 sleep 孙进程并打印其 pid，自身再 wait
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", "sleep 30 & echo $!; wait",
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
    grandchild = int(line.decode().strip())
    os.kill(grandchild, 0)  # 在世（不存在会抛 ProcessLookupError）

    _terminate_process_tree(proc, signal.SIGKILL)
    await asyncio.wait_for(proc.wait(), timeout=5)

    # 轮询等孙进程被 init/launchd 回收消失
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("孙进程在进程组 kill 后仍存活，未被回收")
