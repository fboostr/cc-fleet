"""可插拔 runner 抽象：把驱动 AI coding CLI 子进程的逻辑与具体工具解耦。

- ``base``：公共类型与接口（``AgentPermission``/``AgentRunResult``/``AgentRunner``/
  ``GuardrailHandle``/``GuardrailProvider``/``CallableRunner``）。
- ``engine``：工具无关的子进程引擎 ``run_subprocess`` + ``StreamInterpreter``。
- ``claude``：Claude Code 实现（``ClaudeRunner``/``ClaudeInterpreter``/``ClaudeGuardrailProvider``
  + back-compat ``run_claude``/``build_claude_args`` 等）。

目前仅实现 claude；codex / opencode 待接入，扩展步骤见 ``docs/architecture.md``。
"""

from __future__ import annotations

from .base import (
    AgentPermission,
    AgentRunner,
    AgentRunResult,
    CallableRunner,
    ClaudeRunResult,
    EventCallback,
    GuardrailHandle,
    GuardrailProvider,
    PermissionMode,
)
from .claude import (
    LENGTH_ERROR_HINT,
    ClaudeGuardrailProvider,
    ClaudeInterpreter,
    ClaudeRunner,
    build_claude_args,
    classify_length_error,
    format_run_failure,
    run_claude,
)
from .engine import EngineResult, StreamInterpreter, run_subprocess

__all__ = [
    "AgentPermission",
    "AgentRunner",
    "AgentRunResult",
    "CallableRunner",
    "ClaudeGuardrailProvider",
    "ClaudeInterpreter",
    "ClaudeRunResult",
    "ClaudeRunner",
    "EngineResult",
    "EventCallback",
    "GuardrailHandle",
    "GuardrailProvider",
    "LENGTH_ERROR_HINT",
    "PermissionMode",
    "StreamInterpreter",
    "build_claude_args",
    "classify_length_error",
    "format_run_failure",
    "run_claude",
    "run_subprocess",
]
