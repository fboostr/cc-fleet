"""opencode 专属：argv 拼装、JSON 事件解析、OpencodeRunner / 护栏 provider。

与 ``claude.py`` / ``codex.py`` 对称的第三个工具实现，收口到共享引擎
``engine.run_subprocess``。事件 schema 依据 opencode 1.17.15 **实测**（真跑抓取，
见 ``tests/runners/test_opencode_runner.py`` 的 fixture）：

- 每条事件顶层都带 ``sessionID``（``ses_`` 前缀、**大小写混合**——依赖横切层放宽后的
  sid 正则原样 round-trip），首条即可捕获（**捕获式** id，resume 用 ``--session <id>``）。
- ``text``：一段 assistant 文本在 part **完成时**发一条（实测长文本也只发一条），
  正文在 ``part.text``。
- ``tool_use``：一次工具调用**只在结束时**发一条（``part.state.status`` 为
  completed / error），运行期间流完全静默——因此「工具在飞」不能靠它，而用
  ``step_start`` / ``step_finish``（按 ``part.messageID`` 配对）覆盖整个 step 窗口，
  长命令期间吃宽松的 ``tool_sec`` 档、不被空闲监控误杀。
- ``error``：终态失败（实测随后进程即退、exit=1），错误文本在
  ``error.data.message``（兜底 ``error.name``）。工具级失败（``tool_use`` 的
  status=error）**不是**终态——模型会自行处理继续跑。

护栏：**纯 prompt 软防护**（用户拍板不上 JS 插件）。READ_ONLY 用内置只读
``--agent plan``；WRITE 用 ``--agent build --auto``（自动放行权限）——写档下越界写 /
force-push / 敏感读**均无机械拦截**，靠注入 prompt 的纪律条款自律，启动期由
``AppConfig.validate_runtime`` 发 WARN 点明。仅建议内部信任环境使用。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ...config.schema import OpencodeConfig
from .base import (
    AgentPermission,
    ClaudeRunResult,
    EventCallback,
    GuardrailHandle,
    TimeoutPolicy,
)
from .engine import run_subprocess

logger = logging.getLogger(__name__)

# 写档时前置进协议文本的纪律条款。opencode 写档零机械护栏（比 codex 还少一层 sandbox），
# 条款是唯一防线，措辞比 codex 版更硬。读档由内置 plan agent 限权，不注入以减噪音。
_OPENCODE_WRITE_GUARD_CLAUSE = """\
## 安全纪律（opencode 环境专属，优先级最高、无任何机械兜底）

- **只在当前工作目录（cwd）内创建 / 修改文件**；本环境没有沙箱，越界写不会被拦截，\
但属严重违规，绝对禁止
- **严禁 `git push --force` / `--force-with-lease`**，严禁改写远端历史
- **严禁读取 / 外传 `~/.ssh`、`~/.aws`、`~/.config` 等 cwd 之外的敏感路径**
"""


def build_opencode_args(
    *,
    binary: str,
    permission: AgentPermission,
    resume_from: str | None,
    model: str | None,
) -> list[str]:
    """纯函数，便于测。

    - message 经 stdin 喂入（无位置参数时 opencode 自动读 stdin，实测），规避 ARG_MAX。
    - READ_ONLY → 内置只读 ``--agent plan``；WRITE → ``--agent build --auto``
      （1.17.15 没有 ``--dangerously-skip-permissions``，非交互下放行权限靠 ``--auto``）。
    - 续聊 ``--session <id>``（id 由 opencode 分配、首跑捕获）。
    """
    args: list[str] = [binary, "run", "--format", "json"]
    if resume_from:
        args += ["--session", resume_from]
    if permission is AgentPermission.WRITE:
        args += ["--agent", "build", "--auto"]
    else:
        args += ["--agent", "plan"]
    if model:
        args += ["--model", model]
    return args


class OpencodeInterpreter:
    """opencode ``--format json`` 事件解析（实现 ``engine.StreamInterpreter``）。

    有状态：``text`` 事件按 ``part.id`` 去重（实测每 part 只发一条完成事件，去重是
    对「同 part 增量重发」形态的防御——若上游行为变化，后到的完整文本原地覆盖）。
    """

    def __init__(self) -> None:
        self._part_index: dict[str, int] = {}

    def consume(self, evt: dict, text_parts: list[str]) -> None:
        if evt.get("type") != "text":
            return
        part = evt.get("part")
        if not isinstance(part, dict):
            return
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return
        pid = part.get("id")
        if isinstance(pid, str) and pid in self._part_index:
            text_parts[self._part_index[pid]] = text
            return
        if isinstance(pid, str):
            self._part_index[pid] = len(text_parts)
        text_parts.append(text)

    def session_id(self, evt: dict) -> str | None:
        sid = evt.get("sessionID")
        if isinstance(sid, str) and sid:
            return sid
        return None

    def terminal_error(self, events: list[dict]) -> tuple[bool, str | None]:
        """反向扫：先遇到 ``error`` 即终态失败；先遇到 ``step_finish`` / ``text``
        说明错误之后仍有正常推进（内部已恢复），不判失败。"""
        for evt in reversed(events):
            etype = evt.get("type")
            if etype == "error":
                err = evt.get("error")
                msg: str | None = None
                if isinstance(err, dict):
                    data = err.get("data")
                    if isinstance(data, dict) and isinstance(data.get("message"), str):
                        msg = data["message"].strip() or None
                    if msg is None and isinstance(err.get("name"), str):
                        msg = err["name"]
                return True, msg
            if etype in ("step_finish", "text"):
                return False, None
        return False, None

    def tool_activity(self, evt: dict) -> list[tuple[str, str]]:
        """``step_start`` / ``step_finish`` 按 ``part.messageID`` 配对。

        工具运行期间流完全静默（``tool_use`` 只在结束时发一条），无法按单个工具配对；
        step 窗口覆盖「LLM 调用 + 其间全部工具执行」，in-flight 期间吃 ``tool_sec``
        宽松档——比 claude 的逐工具配对粗，但方向安全（宁可放宽、不误杀长命令）。
        """
        etype = evt.get("type")
        if etype not in ("step_start", "step_finish"):
            return []
        part = evt.get("part")
        if not isinstance(part, dict):
            return []
        mid = part.get("messageID")
        if not isinstance(mid, str):
            return []
        return [("start" if etype == "step_start" else "end", mid)]


class OpencodeGuardrailProvider:
    """opencode 护栏 provider：**纯 prompt 软防护**（已拍板不上 JS 插件）。

    无 settings 文件、无额外 CLI 旗、无 env——唯一防线是 Runner 写档时前置的纪律条款
    与内置 plan agent 的只读限权；差距在启动期 WARN 与文档中显式标注。
    """

    def prepare(self, *, settings_dir: Path) -> GuardrailHandle:
        return GuardrailHandle(settings_path=None, extra_cli_args=[], env={})


class OpencodeRunner:
    """归一 ``AgentRunner`` 接口的 opencode 实现。

    与 claude 的关键差异（与 codex 同型）：``protocol_text`` 前置拼进 prompt（无
    ``--append-system-prompt``）；``session_id`` 形参不使用（id 由 opencode 分配、
    从事件流捕获）；最终文本 = 流内 ``text`` part 拼接（无 last-message 文件）。
    """

    def __init__(self, cfg: OpencodeConfig) -> None:
        self._binary = cfg.binary
        self._model = cfg.model
        self.guardrail = OpencodeGuardrailProvider()

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
        timeout: TimeoutPolicy,
        stream_log_path: Path,
        extra_env: dict[str, str] | None,
        on_event: EventCallback | None,
        kill_event: asyncio.Event | None = None,
    ) -> ClaudeRunResult:
        args = build_opencode_args(
            binary=self._binary,
            permission=permission,
            resume_from=resume_from,
            model=self._model,
        )
        args += list(guardrail.extra_cli_args)

        parts: list[str] = []
        if permission is AgentPermission.WRITE:
            parts.append(_OPENCODE_WRITE_GUARD_CLAUSE)
        if protocol_text:
            parts.append(protocol_text)
        parts.append(prompt)
        full_prompt = "\n\n".join(p for p in parts if p.strip())

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        if guardrail.env:
            env.update(guardrail.env)

        logger.info("启动 opencode：%s", " ".join(args[:8]) + " ...")
        res = await run_subprocess(
            argv=args,
            cwd=cwd,
            stdin_text=full_prompt,
            env=env,
            timeout=timeout,
            stream_log_path=stream_log_path,
            interpreter=OpencodeInterpreter(),
            on_event=on_event,
            kill_event=kill_event,
        )

        return ClaudeRunResult(
            exit_code=res.exit_code,
            session_id=res.init_session_id or resume_from or "",
            text_output=res.text_output,
            stream_log_path=stream_log_path,
            stderr_tail=res.stderr_tail,
            timed_out=res.timed_out,
            events=res.events,
            result_is_error=res.result_is_error,
            error_message=res.error_message,
            timeout_kind=res.timeout_kind,
            killed=res.killed,
        )
