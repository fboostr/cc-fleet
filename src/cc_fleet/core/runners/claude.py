"""Claude Code CLI 专属：argv 拼装、stream 事件解析、ClaudeRunner / 护栏 provider。

从原 claude 子进程封装中抽出 claude 专属部分；工具无关的子进程引擎见 ``engine.py``，
公共类型与接口见 ``base.py``。``run_claude`` 保留为 back-compat 薄组合（签名 / 行为
逐字节不变），``ClaudeRunner`` 是归一 ``AgentRunner`` 接口的 claude 实现。
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
from pathlib import Path

from ...security.permissions import write_settings
from .base import (
    AgentPermission,
    ClaudeRunResult,
    EventCallback,
    GuardrailHandle,
    PermissionMode,
    TimeoutPolicy,
)
from .engine import run_subprocess

logger = logging.getLogger(__name__)


def _terminal_result_error(events: list[dict]) -> tuple[bool, str | None]:
    """从事件流里反向找终态 ``result`` 事件，抽出 ``is_error`` 与人读错误文本。

    模型层失败（如 tool call 无法解析）以 ``is_error=true`` 出现在该事件里，
    错误文本在 ``result`` / ``text`` 字段；非失败时返回 ``(False, None)``。
    """
    for evt in reversed(events):
        if evt.get("type") == "result":
            is_error = bool(evt.get("is_error"))
            msg = evt.get("result") or evt.get("text")
            if is_error and isinstance(msg, str) and msg.strip():
                return True, msg.strip()
            return is_error, None
    return False, None


# 上下文/长度类失败的人读原因（供失败上报点明根因、给处置建议，而非只甩原始报错）。
LENGTH_ERROR_HINT = "建议拆分需求、精简 plan 或缩小改动范围后重试。"

# 模型层「输入过长 / 超出上下文窗口」错误文本的启发式关键字（大小写不敏感）。
# Anthropic 真实文案形如 "prompt is too long: 215000 tokens > 200000 maximum"；
# 这里多留几个同义说法兜底，确切字样以一次超长输入实跑确认后再收敛。
_LENGTH_ERROR_KEYWORDS = (
    "prompt is too long",
    "too long:",
    "context window",
    "context length",
    "maximum context",
    "context_length_exceeded",
    "too many tokens",
    "exceeds the maximum",
    "input length and",
)


def classify_length_error(source: "ClaudeRunResult | BaseException | None") -> str | None:
    """识别「长度 / 上下文过长」类失败，命中则返回人话原因，否则 ``None``。

    两类来源：
    - ``OSError`` E2BIG（命令行参数过长）——prompt 改走 stdin 后基本不会再触发，作兜底保留。
    - 模型层 ``result`` 事件的 ``error_message`` 命中上下文超限关键字（``ClaudeRunResult``
      须 ``result_is_error`` 为真；也接受直接传入异常对象取其文本）。
    """
    if isinstance(source, OSError):
        if source.errno == errno.E2BIG or "argument list too long" in str(source).lower():
            return "命令行参数过长（plan/prompt 体量超出 OS 命令行上限）"
        return None
    if isinstance(source, ClaudeRunResult):
        if not source.result_is_error:
            return None
        msg = source.error_message
    elif isinstance(source, BaseException):
        msg = str(source)
    else:
        return None
    if not msg:
        return None
    low = msg.lower()
    if any(k in low for k in _LENGTH_ERROR_KEYWORDS):
        return "plan/上下文过长，超出模型上下文窗口（约 200K token）"
    return None


def format_run_failure(result: ClaudeRunResult, phase: str) -> str:
    """拼装一次 claude 运行失败的上报文本。

    优先用 result 事件里的人读错误（``error_message``，模型层失败的真实根因），
    其次回退到 stderr 尾部；exit_code 非零时附带退出码。这样即便 stderr 为空、
    仅 result 事件带 ``is_error`` 的瞬时失败也能自解释，而不是只报 ``exit=N``。
    若判定为「长度 / 上下文过长」类失败，额外追加一行平实说明 + 处置建议。
    """
    head = f"{phase} 阶段失败"
    if result.exit_code not in (0, None):
        head += f"：exit={result.exit_code}"
    reason = classify_length_error(result)
    if reason:
        head += f"\n⚠️ {reason}。{LENGTH_ERROR_HINT}"
    detail = result.error_message or (
        result.stderr_tail[-500:] if result.stderr_tail.strip() else ""
    )
    return f"{head}\n{detail}".rstrip()


class ClaudeInterpreter:
    """claude stream-json 事件解析（实现 ``engine.StreamInterpreter``）。"""

    def consume(self, evt: dict, text_parts: list[str]) -> None:
        # assistant 文本提取：兼容两种常见形态，按出现顺序累积（不去重）
        etype = evt.get("type")
        if etype == "assistant":
            msg = evt.get("message", {})
            for block in msg.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
        elif etype == "result":
            # 终态 result 通常携带最终 text，兜底再追加一次（去重避免与 assistant 重复）
            final = evt.get("result") or evt.get("text")
            if isinstance(final, str) and final not in text_parts:
                text_parts.append(final)

    def session_id(self, evt: dict) -> str | None:
        if evt.get("type") == "system" and evt.get("subtype") == "init":
            sid = evt.get("session_id")
            if isinstance(sid, str):
                return sid
        return None

    def terminal_error(self, events: list[dict]) -> tuple[bool, str | None]:
        return _terminal_result_error(events)

    def tool_activity(self, evt: dict) -> list[tuple[str, str]]:
        """claude 的工具生命周期：``assistant`` 消息里的 ``tool_use`` block（发起，按 ``id``）、
        ``user`` 消息里的 ``tool_result`` block（结果已回，按 ``tool_use_id`` 配对）。

        一条事件可含多个 block（并行工具调用 / 批量结果），逐个抽出，供引擎判「工具在飞」。
        """
        etype = evt.get("type")
        content = (evt.get("message") or {}).get("content")
        if not isinstance(content, list):
            return []
        out: list[tuple[str, str]] = []
        if etype == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id")
                    if isinstance(tid, str):
                        out.append(("start", tid))
        elif etype == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if isinstance(tid, str):
                        out.append(("end", tid))
        return out


def build_claude_args(
    *,
    binary: str,
    session_id: str,
    permission_mode: PermissionMode,
    resume_from: str | None,
    settings_path: Path | None,
    append_system_prompt_file: Path | None,
) -> list[str]:
    """纯函数，便于测。

    注意：prompt 不在 argv 里——它经子进程 stdin 传入（见 ``run_claude``），以规避
    OS 命令行参数上限（Linux 单参 128 KiB / macOS argv+env 合计 1 MiB）。``-p`` 此处
    仅作 print（非交互）模式开关，不带位置参数。
    """
    args: list[str] = [binary, "-p"]
    args += ["--permission-mode", permission_mode]
    args += ["--output-format", "stream-json", "--include-partial-messages", "--verbose"]
    args += ["--dangerously-skip-permissions"]
    if resume_from:
        args += ["--resume", resume_from]
    else:
        args += ["--session-id", session_id]
    if settings_path is not None:
        args += ["--settings", str(settings_path)]
    if append_system_prompt_file is not None:
        # 没有 --append-system-prompt-file 标志，回退读文件后用 --append-system-prompt 传入
        try:
            content = Path(append_system_prompt_file).read_text(encoding="utf-8")
        except OSError:
            content = ""
        if content:
            args += ["--append-system-prompt", content]
    return args


async def _drive_claude(
    *,
    argv: list[str],
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    timeout: TimeoutPolicy,
    stream_log_path: Path,
    on_event: EventCallback | None,
    resume_from: str | None,
    session_id: str,
    kill_event: asyncio.Event | None = None,
) -> ClaudeRunResult:
    """共享：起 claude 子进程（经共享引擎）+ 解析 + 组装 ``ClaudeRunResult``。

    ``run_claude``（back-compat）与 ``ClaudeRunner.run``（归一接口）都收口到此，确保
    两条路径的子进程行为与 session_id 回退策略逐字节一致。
    """
    logger.info("启动 claude：%s", " ".join(argv[:6]) + " ...")
    res = await run_subprocess(
        argv=argv,
        cwd=cwd,
        stdin_text=prompt,
        env=env,
        timeout=timeout,
        stream_log_path=stream_log_path,
        interpreter=ClaudeInterpreter(),
        on_event=on_event,
        kill_event=kill_event,
    )
    effective_sid = res.init_session_id or resume_from or session_id
    return ClaudeRunResult(
        exit_code=res.exit_code,
        session_id=effective_sid,
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


async def run_claude(
    *,
    binary: str,
    prompt: str,
    cwd: Path,
    session_id: str,
    permission_mode: PermissionMode,
    stream_log_path: Path,
    timeout_sec: int,
    resume_from: str | None = None,
    settings_path: Path | None = None,
    append_system_prompt_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
    on_event: EventCallback | None = None,
) -> ClaudeRunResult:
    """启动一次 claude 子进程，等待结束或超时后返回（back-compat 薄组合，签名不变）。

    prompt 经 **stdin** 喂入（不走 argv 的 ``-p`` 位置参数），以规避 OS 命令行参数上限：
    plan 暴涨时单个 ``-p`` 参数会顶爆 ARG_MAX（Linux 单参硬上限 128 KiB）让子进程根本
    起不来。stdin 是流、无此限制，上限抬到模型上下文窗口（撞线时是干净的模型层报错）。

    ``timeout_sec`` 保持标量签名以兼容老调用方——内部转成三档相等的 ``TimeoutPolicy``，
    等价旧「总时长上界」墙钟语义（``hard_cap`` 兜住）。走空闲分档的新路径请用 ``ClaudeRunner``。
    """
    args = build_claude_args(
        binary=binary,
        session_id=session_id,
        permission_mode=permission_mode,
        resume_from=resume_from,
        settings_path=settings_path,
        append_system_prompt_file=append_system_prompt_file,
    )
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return await _drive_claude(
        argv=args,
        prompt=prompt,
        cwd=cwd,
        env=env,
        timeout=TimeoutPolicy(
            idle_sec=timeout_sec, tool_sec=timeout_sec, hard_cap_sec=timeout_sec
        ),
        stream_log_path=stream_log_path,
        on_event=on_event,
        resume_from=resume_from,
        session_id=session_id,
    )


class ClaudeGuardrailProvider:
    """claude 护栏 provider：生成挂 PreToolUse hook 的 settings.json。

    包住 ``security.permissions.write_settings``；白名单 env（``CC_FLEET_WORKTREE`` 等）
    当前仍由 session 经 ``extra_env`` 注入，故 ``GuardrailHandle.env`` 在此为空。
    """

    def prepare(self, *, settings_dir: Path) -> GuardrailHandle:
        settings_path = write_settings(settings_dir)
        return GuardrailHandle(settings_path=settings_path, extra_cli_args=[], env={})


class ClaudeRunner:
    """归一 ``AgentRunner`` 接口的 claude 实现。

    把归一参数翻译回 claude 旗标：``permission`` → ``--permission-mode``、
    ``protocol_text`` → ``--append-system-prompt``、``guardrail.settings_path`` →
    ``--settings``；再收口到与 ``run_claude`` 同一条 ``_drive_claude`` 引擎路径。
    """

    def __init__(self, binary: str) -> None:
        self._binary = binary
        self.guardrail = ClaudeGuardrailProvider()

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
        mode: PermissionMode = (
            "plan" if permission is AgentPermission.READ_ONLY else "acceptEdits"
        )
        # protocol_text 已是文本，直接当 --append-system-prompt 内容（省去临时文件往返）；
        # build_claude_args 的 append_system_prompt_file 留 None。
        args = build_claude_args(
            binary=self._binary,
            session_id=session_id,
            permission_mode=mode,
            resume_from=resume_from,
            settings_path=guardrail.settings_path,
            append_system_prompt_file=None,
        )
        args += list(guardrail.extra_cli_args)
        if protocol_text:
            args += ["--append-system-prompt", protocol_text]
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        if guardrail.env:
            env.update(guardrail.env)
        return await _drive_claude(
            argv=args,
            prompt=prompt,
            cwd=cwd,
            env=env,
            timeout=timeout,
            stream_log_path=stream_log_path,
            on_event=on_event,
            resume_from=resume_from,
            session_id=session_id,
            kill_event=kill_event,
        )
