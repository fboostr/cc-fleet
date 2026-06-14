"""ClaudeInterpreter 抽取 + ClaudeRunner.run 归一参数映射（P1 新增）。"""

from __future__ import annotations

from pathlib import Path

from cc_fleet.core.runners import claude as claude_mod
from cc_fleet.core.runners.base import AgentPermission, GuardrailHandle
from cc_fleet.core.runners.claude import (
    ClaudeGuardrailProvider,
    ClaudeInterpreter,
    ClaudeRunner,
)
from cc_fleet.core.runners.engine import EngineResult


# ---- ClaudeInterpreter ----


def test_interpreter_assistant_text_not_deduped():
    """assistant 文本按出现顺序累积、不去重（逐字节复刻旧 _process_line）。"""
    it = ClaudeInterpreter()
    parts: list[str] = []
    it.consume({"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}}, parts)
    it.consume({"type": "assistant", "message": {"content": [{"type": "text", "text": "b"}]}}, parts)
    assert parts == ["a", "b"]


def test_interpreter_result_text_deduped():
    """终态 result 文本若与已累积内容重复则不再追加。"""
    it = ClaudeInterpreter()
    parts = ["done"]
    it.consume({"type": "result", "result": "done"}, parts)
    assert parts == ["done"]


def test_interpreter_session_id_from_init():
    it = ClaudeInterpreter()
    assert it.session_id({"type": "system", "subtype": "init", "session_id": "s1"}) == "s1"
    assert it.session_id({"type": "assistant"}) is None


def test_interpreter_terminal_error():
    it = ClaudeInterpreter()
    is_err, msg = it.terminal_error([{"type": "result", "is_error": True, "result": "boom"}])
    assert is_err is True and msg == "boom"


# ---- ClaudeRunner.run 参数映射 ----


def _engine_result(**ov) -> EngineResult:
    base = dict(
        exit_code=0,
        text_output="ok",
        init_session_id=None,
        stderr_tail="",
        timed_out=False,
        events=[],
        result_is_error=False,
        error_message=None,
    )
    base.update(ov)
    return EngineResult(**base)


async def test_run_maps_read_only_protocol_and_guardrail(monkeypatch, tmp_path: Path):
    """READ_ONLY→--permission-mode plan；protocol_text→--append-system-prompt；
    guardrail.settings_path→--settings；extra_env 与 guardrail.env 都进子进程 env；
    init_session_id 优先作为 effective session_id。"""
    captured: dict = {}

    async def fake_run_subprocess(
        *, argv, cwd, stdin_text, env, timeout_sec, stream_log_path, interpreter, on_event=None
    ):
        captured.update(argv=argv, env=env, stdin=stdin_text)
        return _engine_result(init_session_id="init-sid")

    monkeypatch.setattr(claude_mod, "run_subprocess", fake_run_subprocess)

    runner = ClaudeRunner(binary="claude")
    gr = GuardrailHandle(settings_path=tmp_path / "s.json", extra_cli_args=[], env={"GX": "1"})
    res = await runner.run(
        prompt="hi",
        cwd=tmp_path,
        permission=AgentPermission.READ_ONLY,
        protocol_text="PROTO",
        session_id="sid",
        resume_from=None,
        guardrail=gr,
        timeout_sec=10,
        stream_log_path=tmp_path / "log",
        extra_env={"EX": "2"},
        on_event=None,
    )
    argv = captured["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--settings") + 1] == str(tmp_path / "s.json")
    assert argv[argv.index("--append-system-prompt") + 1] == "PROTO"
    assert "--session-id" in argv  # resume_from None → 用 --session-id
    assert captured["env"]["EX"] == "2" and captured["env"]["GX"] == "1"
    assert captured["stdin"] == "hi"
    assert res.session_id == "init-sid"


async def test_run_maps_write_resume_and_empty_protocol(monkeypatch, tmp_path: Path):
    """WRITE→acceptEdits；resume_from 非空→--resume 且不带 --session-id；
    protocol_text 空 → 不加 --append-system-prompt；settings_path None → 不加 --settings；
    init None 时 effective session_id 回退到 resume_from。"""
    captured: dict = {}

    async def fake_run_subprocess(*, argv, **kw):
        captured["argv"] = argv
        return _engine_result(init_session_id=None)

    monkeypatch.setattr(claude_mod, "run_subprocess", fake_run_subprocess)

    runner = ClaudeRunner(binary="claude")
    gr = GuardrailHandle(settings_path=None, extra_cli_args=[], env={})
    res = await runner.run(
        prompt="x",
        cwd=tmp_path,
        permission=AgentPermission.WRITE,
        protocol_text="",
        session_id="sid",
        resume_from="rsid",
        guardrail=gr,
        timeout_sec=5,
        stream_log_path=tmp_path / "l",
        extra_env=None,
        on_event=None,
    )
    argv = captured["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--resume") + 1] == "rsid"
    assert "--session-id" not in argv
    assert "--append-system-prompt" not in argv
    assert "--settings" not in argv
    assert res.session_id == "rsid"


# ---- 接口形状（鸭子检查 AgentRunner / GuardrailProvider）----


def test_claude_runner_and_guardrail_shape():
    r = ClaudeRunner(binary="claude")
    assert callable(getattr(r, "run", None))
    assert hasattr(r, "guardrail")
    assert callable(getattr(r.guardrail, "prepare", None))
    assert callable(getattr(ClaudeGuardrailProvider(), "prepare", None))
