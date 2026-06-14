"""Runner 抽象层的公共类型与接口。

把「驱动一个 AI coding CLI 子进程」抽象成可插拔 runner，为后续接入
Codex / opencode 铺路。P1 只有 claude 一个实现（见 ``claude.py``）；其它工具在
后续阶段新增，复用本模块定义的接口与结果类型。

- ``AgentPermission``：归一的读写权限，取代 claude 专属的 ``plan``/``acceptEdits`` 字面量。
- ``AgentRunResult``（= ``ClaudeRunResult``）：一次运行的结果，字段对所有工具通用。
- ``GuardrailHandle`` / ``GuardrailProvider``：护栏配置（settings / cli 旗标 / env）。
- ``AgentRunner``：runner 接口；``ClaudeRunner`` 等逐工具实现它。
- ``CallableRunner``：把一个 async callable 适配成 ``AgentRunner``（测试注入用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Literal, Protocol

# claude CLI 的 ``--permission-mode`` 取值。归一接口对外用 ``AgentPermission``，
# 这个字面量类型只在 ``build_claude_args`` 等 claude 专属处内部使用。
PermissionMode = Literal["plan", "acceptEdits", "default", "bypassPermissions"]

# 每解析出一条 stream 事件就 await 触发一次的回调。
EventCallback = Callable[[dict], Awaitable[None]]


class AgentPermission(Enum):
    """归一的运行权限。各工具 runner 内部映射到自己的旗标。

    - ``READ_ONLY``：只读 / 计划模式（claude → ``--permission-mode plan``）。
    - ``WRITE``：可写 / 可编辑（claude → ``--permission-mode acceptEdits``）。
    """

    READ_ONLY = "read_only"
    WRITE = "write"


@dataclass
class ClaudeRunResult:
    """一次 agent 子进程运行的结果。

    字段对所有工具通用，故 ``AgentRunResult`` 是它的别名；保留 ``ClaudeRunResult``
    主名是为兼容现有 import 与测试。
    """

    exit_code: int | None
    session_id: str
    text_output: str
    stream_log_path: Path
    stderr_tail: str
    timed_out: bool
    events: list[dict] = field(default_factory=list)
    # 终态 result 事件的失败标记与人读错误文本。模型层失败（如 tool call 无法解析）
    # 会以 is_error=true 出现在 stdout 的 stream-json result 事件里，stderr 往往为空——
    # 单看 exit_code/stderr_tail 会把根因丢掉，故在此单独抽出供失败上报使用。
    result_is_error: bool = False
    error_message: str | None = None


# 结果类型对所有工具通用，别名让后续工具的 runner 不必引用 claude 命名。
AgentRunResult = ClaudeRunResult


@dataclass
class GuardrailHandle:
    """一次运行的护栏配置产物。

    - ``settings_path``：claude 的 ``--settings <settings.json>``（含 PreToolUse hook）；
      codex/opencode 不用此机制时为 ``None``。
    - ``extra_cli_args``：追加到 argv 的护栏旗标（claude 恒空；codex 映射 ``--sandbox`` 等）。
    - ``env``：注入子进程的护栏环境变量（claude 的白名单 env 当前仍由 session 经
      ``extra_env`` 传，故此处恒空；保留字段供后续工具使用）。
    """

    settings_path: Path | None
    extra_cli_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


class GuardrailProvider(Protocol):
    """逐工具的护栏 provider。输入护栏原料，产出 ``GuardrailHandle``。"""

    def prepare(self, *, settings_dir: Path) -> GuardrailHandle: ...


class AgentRunner(Protocol):
    """驱动一个 AI coding CLI 的 runner 接口。逐工具实现。

    ``guardrail`` 是本 runner 配套的护栏 provider；调用方（session）先用它把护栏
    原料（settings 目录等）prepare 成 ``GuardrailHandle``，再连同 ``run`` 一起喂入。
    """

    guardrail: GuardrailProvider

    async def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        permission: AgentPermission,
        protocol_text: str,
        session_id: str,
        resume_from: str | None,
        guardrail: GuardrailHandle,
        timeout_sec: int,
        stream_log_path: Path,
        extra_env: dict[str, str] | None,
        on_event: EventCallback | None,
    ) -> AgentRunResult: ...


class CallableRunner:
    """把一个 ``async callable(**kwargs) -> AgentRunResult`` 适配成 ``AgentRunner``。

    仅用于测试：现有用例通过 ``Session(claude_run=stub)`` 注入一个按 ``**kwargs``
    返回脚本化结果的假 runner。``run`` 原样转发 kwargs；``guardrail`` 默认用 claude 的
    provider（延迟 import 避免与 ``claude.py`` 形成 import 环），从而保持「调用点照常
    生成 settings.json」这一与现状逐字节一致的副作用。
    """

    def __init__(
        self,
        fn: Callable[..., Awaitable[AgentRunResult]],
        guardrail: GuardrailProvider | None = None,
    ) -> None:
        self._fn = fn
        self._guardrail = guardrail

    @property
    def guardrail(self) -> GuardrailProvider:
        if self._guardrail is None:
            from .claude import ClaudeGuardrailProvider

            self._guardrail = ClaudeGuardrailProvider()
        return self._guardrail

    async def run(self, **kwargs) -> AgentRunResult:
        return await self._fn(**kwargs)
