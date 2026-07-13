"""Codex CLI 专属：argv 拼装、JSONL 事件解析、CodexRunner / 护栏 provider。

与 ``claude.py`` 对称的第二个工具实现，收口到同一个共享引擎 ``engine.run_subprocess``。
事件 schema 依据 codex-cli 0.142.5 实测 JSONL（见 ``tests/runners/test_codex_runner.py``
的 fixture）：

- ``thread.started``：``thread_id`` 即会话 id（**捕获式**——codex 不接受外部指定 id，
  首跑后由编排层回写落库，resume 用 ``codex exec resume <thread_id>``）。
- ``item.started`` / ``item.completed``：一次工具调用 / 一段 agent 消息的生命周期；
  ``item.type == "agent_message"`` 的 ``text`` 是模型输出文本。
- ``error``：**瞬态**错误（如网络重连中），不代表本轮失败——终态失败只看 ``turn.failed``。
- ``turn.completed`` / ``turn.failed``：一轮的终态；失败时错误文本在 ``error.message``。

护栏走 OS sandbox：读档 ``--sandbox read-only``、写档 ``--sandbox workspace-write``（写
被内核限制在 cwd 内）。已知缺口（按「尽力而为 + 显式标注」拍板）：sandbox 只管文件写，
**拦不住 force-push** 等网络操作，也不拦敏感目录**读取**——这两条靠 prompt 条款自律，
启动期由 ``AppConfig.validate_runtime`` 发 WARN 提示。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ...config.schema import CodexConfig
from .base import (
    AgentPermission,
    ClaudeRunResult,
    EventCallback,
    GuardrailHandle,
    TimeoutPolicy,
)
from .engine import run_subprocess

logger = logging.getLogger(__name__)

# 沙箱档位：归一权限 → codex --sandbox 值
_SANDBOX_BY_PERMISSION = {
    AgentPermission.READ_ONLY: "read-only",
    AgentPermission.WRITE: "workspace-write",
}

# 写档时前置进协议文本的 codex 专属纪律条款：sandbox 管不到的部分（网络操作、敏感读）
# 靠模型自律。读档无写能力，不注入以减噪音。
_CODEX_WRITE_GUARD_CLAUSE = """\
## 安全纪律（codex 环境专属，优先级最高）

- 只在当前工作目录（cwd）内创建 / 修改文件；目录外写入会被沙箱拦截，不要尝试绕过
- **严禁 `git push --force` / `--force-with-lease`**；沙箱不拦网络操作，此为硬性纪律
- 不要读取 `~/.ssh`、`~/.aws`、`~/.config` 等 cwd 之外的敏感目录
"""


def build_codex_args(
    *,
    binary: str,
    sandbox_mode: str,
    resume_from: str | None,
    last_message_path: Path,
    model: str | None,
) -> list[str]:
    """纯函数，便于测。

    - prompt 一律经 stdin 喂入（末位 ``-`` 哨兵；``codex exec`` 省略 prompt 也读 stdin，
      但 ``resume`` 子命令省略即「无 prompt」，显式 ``-`` 两条路径行为统一），规避 ARG_MAX。
    - 首跑：``codex exec --sandbox <mode> ...``（不传会话 id，codex 自行分配、事后捕获）；
      续聊：``codex exec resume <sid> ...``——resume 子命令**没有** ``--sandbox`` flag，
      档位经 ``-c sandbox_mode=`` 配置覆盖传入（plan 只读会话续到 dev 写档时靠它提权）。
    - ``--output-last-message``：最终回复的权威落点（流内 agent_message 文本作兜底）。
    """
    args: list[str] = [binary, "exec"]
    if resume_from:
        args += ["resume", resume_from, "-c", f'sandbox_mode="{sandbox_mode}"']
    else:
        args += ["--sandbox", sandbox_mode]
    args += ["--json", "--output-last-message", str(last_message_path)]
    if model:
        args += ["--model", model]
    args += ["-"]
    return args


class CodexInterpreter:
    """codex ``--json`` JSONL 事件解析（实现 ``engine.StreamInterpreter``）。"""

    def consume(self, evt: dict, text_parts: list[str]) -> None:
        # agent 消息只在 item.completed 收口一次（item.updated 是增量，收了会重复）
        if evt.get("type") == "item.completed":
            item = evt.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)

    def session_id(self, evt: dict) -> str | None:
        if evt.get("type") == "thread.started":
            tid = evt.get("thread_id")
            if isinstance(tid, str) and tid:
                return tid
        return None

    def terminal_error(self, events: list[dict]) -> tuple[bool, str | None]:
        """只认 ``turn.failed`` 为终态失败；``error`` 事件是瞬态（重连等），不作数。

        实测一次失败轮里会先出现多条 ``error`` 再收 ``turn.failed``，成功轮也可能夹杂
        瞬态 ``error``——故反向找最近的 turn 终态事件，其余一概不判。
        """
        for evt in reversed(events):
            etype = evt.get("type")
            if etype == "turn.failed":
                err = evt.get("error")
                msg = err.get("message") if isinstance(err, dict) else None
                if isinstance(msg, str) and msg.strip():
                    return True, msg.strip()
                return True, None
            if etype == "turn.completed":
                return False, None
        return False, None

    def tool_activity(self, evt: dict) -> list[tuple[str, str]]:
        """``item.started`` / ``item.completed`` 按 ``item.id`` 配对，供引擎判「工具在飞」。

        不区分 item 类型（command_execution / agent_message / ...）：始末事件成对出现，
        长命令期间 in-flight 吃宽松的 ``tool_sec`` 档，正是分档超时想要的。
        """
        etype = evt.get("type")
        item = evt.get("item")
        if not isinstance(item, dict):
            return []
        iid = item.get("id")
        if not isinstance(iid, str):
            return []
        if etype == "item.started":
            return [("start", iid)]
        if etype == "item.completed":
            return [("end", iid)]
        return []


class CodexGuardrailProvider:
    """codex 护栏 provider：护栏由 OS sandbox 承担（``--sandbox`` 档位随 permission 变，
    由 ``CodexRunner.run`` 拼 argv），无 settings 文件、无额外 env。"""

    def prepare(self, *, settings_dir: Path) -> GuardrailHandle:
        return GuardrailHandle(settings_path=None, extra_cli_args=[], env={})


class CodexRunner:
    """归一 ``AgentRunner`` 接口的 codex 实现。

    与 claude 的两点关键差异：
    - ``protocol_text`` **前置拼进 prompt**（codex 无 ``--append-system-prompt``）；写档
      再前置一段沙箱盲区的纪律条款。
    - ``session_id`` 形参不使用（codex 不接受外部 id）；会话 id 从 ``thread.started``
      捕获，``resume_from`` 时走 ``codex exec resume``。
    """

    def __init__(self, cfg: CodexConfig) -> None:
        self._binary = cfg.binary
        self._model = cfg.model
        self.guardrail = CodexGuardrailProvider()

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
        sandbox_mode = _SANDBOX_BY_PERMISSION[permission]
        # 最终回复权威落点，与 stream 日志同目录（如 stream.jsonl → stream.last_message.txt）。
        # 路径跨轮次复用，起进程前先清掉旧文件——否则本轮 codex 崩溃没写文件时，会把
        # 上一轮的陈旧回复误当本轮权威文本。
        last_message_path = stream_log_path.with_suffix(".last_message.txt")
        try:
            last_message_path.unlink(missing_ok=True)
        except OSError:
            pass
        args = build_codex_args(
            binary=self._binary,
            sandbox_mode=sandbox_mode,
            resume_from=resume_from,
            last_message_path=last_message_path,
            model=self._model,
        )
        args += list(guardrail.extra_cli_args)

        parts: list[str] = []
        if permission is AgentPermission.WRITE:
            parts.append(_CODEX_WRITE_GUARD_CLAUSE)
        if protocol_text:
            parts.append(protocol_text)
        parts.append(prompt)
        full_prompt = "\n\n".join(p for p in parts if p.strip())

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        if guardrail.env:
            env.update(guardrail.env)

        logger.info("启动 codex：%s", " ".join(args[:8]) + " ...")
        res = await run_subprocess(
            argv=args,
            cwd=cwd,
            stdin_text=full_prompt,
            env=env,
            timeout=timeout,
            stream_log_path=stream_log_path,
            interpreter=CodexInterpreter(),
            on_event=on_event,
            kill_event=kill_event,
        )

        # 最终文本优先读 --output-last-message 文件（权威），流内拼接作兜底
        text_output = res.text_output
        try:
            authoritative = last_message_path.read_text(encoding="utf-8")
            if authoritative.strip():
                text_output = authoritative
        except OSError:
            pass

        return ClaudeRunResult(
            exit_code=res.exit_code,
            session_id=res.init_session_id or resume_from or "",
            text_output=text_output,
            stream_log_path=stream_log_path,
            stderr_tail=res.stderr_tail,
            timed_out=res.timed_out,
            events=res.events,
            result_is_error=res.result_is_error,
            error_message=res.error_message,
            timeout_kind=res.timeout_kind,
            killed=res.killed,
        )
