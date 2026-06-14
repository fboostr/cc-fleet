"""tests 公共 runner 测试替身与小工具（归一 runner 接口后）。

P1 把 session 的 4 个调用点从旧 ``claude_run(permission_mode=, append_system_prompt_file=)``
归一为 ``runner.run(permission=AgentPermission, protocol_text=, guardrail=)``。本模块提供：

- ``FakeRunner``：把一个 ``async fn(**kwargs) -> ClaudeRunResult`` 适配成 ``AgentRunner``
  （含 no-op guardrail），用于需要分别注入 coder / reviewer runner 的用例。
- ``perm_mode``：从归一 kwargs 的 ``permission`` 反推旧 ``"plan"/"acceptEdits"`` 字面量，
  让既有 stub 基于字符串的判据 / 断言最小改动。
- ``fake_result``：按 stub 收到的 kwargs 快速构造 ``ClaudeRunResult``。
"""

from __future__ import annotations

from cc_fleet.core.runners.base import AgentPermission, ClaudeRunResult, GuardrailHandle


def perm_mode(kwargs: dict) -> str:
    """从归一 kwargs 的 ``permission`` 反推旧 permission_mode 字面量。"""
    return "plan" if kwargs.get("permission") is AgentPermission.READ_ONLY else "acceptEdits"


def fake_result(kwargs: dict, text: str = "", **overrides) -> ClaudeRunResult:
    """按 runner.run 的 kwargs 构造一个 ``ClaudeRunResult``（session_id 复刻真实回退链）。"""
    base = dict(
        exit_code=0,
        session_id=kwargs.get("resume_from") or kwargs["session_id"],
        text_output=text,
        stream_log_path=kwargs["stream_log_path"],
        stderr_tail="",
        timed_out=False,
    )
    base.update(overrides)
    return ClaudeRunResult(**base)


class _NoopGuardrail:
    """测试用护栏 provider：不写 settings.json，返回空 handle。"""

    def prepare(self, *, settings_dir) -> GuardrailHandle:
        return GuardrailHandle(settings_path=None, extra_cli_args=[], env={})


class FakeRunner:
    """把一个 ``async fn(**kwargs) -> ClaudeRunResult`` 适配成 ``AgentRunner``。

    ``run`` 原样转发 kwargs 给 fn；``guardrail`` 为 no-op（测试不依赖 settings.json 落盘）。
    """

    def __init__(self, fn) -> None:
        self._fn = fn
        self.guardrail = _NoopGuardrail()

    async def run(self, **kwargs) -> ClaudeRunResult:
        return await self._fn(**kwargs)
