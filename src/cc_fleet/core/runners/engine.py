"""工具无关的子进程引擎：起 agent CLI 子进程、逐行解析 stream-json、超时回收。

从原 claude 子进程封装中抽出、与具体工具解耦：
- 进程编排：``create_subprocess_exec(start_new_session=True)`` + stdin 写入 + 超时杀进程组。
- stdout：chunk 读避开 asyncio readline 的 64 KiB 行长上限，逐行落盘 + 触发 on_event。
- 「一条事件如何抽文本 / session_id / 终态错误」交给注入的 ``StreamInterpreter``（逐工具）。

claude 的解析见 ``claude.py`` 的 ``ClaudeInterpreter``；后续 codex/opencode 各写一个。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .base import EventCallback

logger = logging.getLogger(__name__)


class StreamInterpreter(Protocol):
    """把一条已解析的 stream 事件翻译成增量文本 / session_id / 终态错误。逐工具实现。"""

    def consume(self, evt: dict, text_parts: list[str]) -> None:
        """按本工具规则把 evt 的文本增量原地追加到 ``text_parts``（含各自的去重策略）。"""

    def session_id(self, evt: dict) -> str | None:
        """从 evt 抽取会话 id（如 claude 的 system/init 事件）；无则 ``None``。"""

    def terminal_error(self, events: list[dict]) -> tuple[bool, str | None]:
        """从事件流抽取终态失败标记与人读错误文本。"""


@dataclass
class EngineResult:
    """``run_subprocess`` 的原始产物（不含工具专属的 session_id 回退策略）。"""

    exit_code: int | None
    text_output: str
    init_session_id: str | None
    stderr_tail: str
    timed_out: bool
    events: list[dict]
    result_is_error: bool
    error_message: str | None


def _escape_leading_slash(prompt: str) -> str:
    """Claude Code CLI 把 `-p` 内容首字符为 `/` 时当 slash 指令解析（如 `/list`），立即
    返回 `Unknown command` 并退出，整轮 plan/dev 都跑不起来。dispatcher 剥掉 `@<repo>`
    前缀后，用户原文以 `/` 起头就会踩这条路径（如 req-20260519-132428-f5ff）。
    在唯一 CLI 调用边界用零宽空格（U+200B）做最小入侵前缀：CLI 不再触发 slash 解析，
    模型读到几乎无感。"""
    if prompt.startswith("/"):
        return "​" + prompt
    return prompt


async def _process_line(
    raw_line: bytes,
    f,
    text_parts: list[str],
    events: list[dict],
    detected_session_id: str | None,
    on_event: EventCallback | None,
    interpreter: StreamInterpreter,
) -> str | None:
    """处理 stream-json 中的一行：落盘 + 解析 + 交 interpreter 抽文本/session_id + 触发 on_event。"""
    line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
    if not line:
        return detected_session_id
    f.write(line + "\n")
    f.flush()
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("非 JSON 行被忽略：%s", line[:200])
        return detected_session_id
    events.append(evt)

    sid = interpreter.session_id(evt)
    if isinstance(sid, str):
        detected_session_id = sid
    interpreter.consume(evt, text_parts)

    if on_event is not None:
        # 回调内的异常不应中断流读取，避免一次写库失败把整个 session 卡死
        try:
            await on_event(evt)
        except Exception:  # noqa: BLE001
            logger.exception("on_event 回调异常，已吞掉以保证 stream 继续")

    return detected_session_id


async def _read_stream_json(
    stdout: asyncio.StreamReader,
    log_path: Path,
    events: list[dict],
    on_event: EventCallback | None = None,
    interpreter: StreamInterpreter | None = None,
) -> tuple[str, str | None]:
    """逐行读取 agent 的 stream-json 输出。

    返回 (text_output, session_id_from_init)。stream 中所有行原样落盘。
    如果传入 on_event，则每解析出一条事件就 await 调用一次，便于上层结构化入库。
    ``interpreter`` 缺省为 ``ClaudeInterpreter``（延迟 import）——历史调用方与单测直接
    调本函数、喂 claude 格式事件并断言 claude 抽取结果。

    用 ``stdout.read(chunk)`` 攒 buffer 再按 ``\\n`` 切，**不走 readline**：asyncio
    StreamReader 默认行长上限 64 KiB，agent 单条 stream-json 事件（大 tool_result、
    巨型 Read 回填、长 thinking 块）可能远超此值，readline 会抛 LimitOverrunError →
    ValueError 把整轮 drive 打挂；chunk 模式行长无上限。
    """
    if interpreter is None:
        from .claude import ClaudeInterpreter

        interpreter = ClaudeInterpreter()
    text_parts: list[str] = []
    detected_session_id: str | None = None
    chunk_size = 64 * 1024

    log_path.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray()
    with log_path.open("a", encoding="utf-8") as f:
        while True:
            chunk = await stdout.read(chunk_size)
            if not chunk:
                # EOF：消化 buf 末尾可能没带 \n 的最后一行
                if buf:
                    detected_session_id = await _process_line(
                        bytes(buf), f, text_parts, events, detected_session_id, on_event, interpreter
                    )
                    buf.clear()
                break
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                detected_session_id = await _process_line(
                    line, f, text_parts, events, detected_session_id, on_event, interpreter
                )

    return "".join(text_parts), detected_session_id


async def _read_stderr(stderr: asyncio.StreamReader, limit: int = 8192) -> str:
    """累积 stderr 末尾，用于失败时定位。"""
    buf = bytearray()
    async for line in stderr:
        buf.extend(line)
        if len(buf) > limit * 2:
            del buf[: len(buf) - limit]
    return buf.decode("utf-8", errors="replace")[-limit:]


def _terminate_process_tree(proc: asyncio.subprocess.Process, sig: int) -> None:
    """给子进程所在**进程组**发信号，连带回收它派生的孙进程。

    ``run_subprocess`` 用 ``start_new_session=True`` 起子进程，使其自成新会话/进程组
    （pgid == pid）。agent CLI 常派生 git / ssh / ``docker exec`` / 测试等子进程；
    只对直接子进程 ``terminate()/kill()`` 会留下游离的孙进程继续跑（被 init 接管，
    泄漏 CPU/连接）。故超时回收时按进程组杀。

    已退出（``returncode`` 非 None）直接跳过；进程组已不存在吞掉；非 POSIX 或拿不到
    进程组时退回只杀直接子进程，保证不因平台差异抛错。
    """
    if proc.returncode is not None:
        return
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    if killpg is not None and getpgid is not None:
        try:
            killpg(getpgid(proc.pid), sig)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass  # 退回直接杀子进程
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass


async def run_subprocess(
    *,
    argv: list[str],
    cwd: Path,
    stdin_text: str,
    env: dict[str, str],
    timeout_sec: int,
    stream_log_path: Path,
    interpreter: StreamInterpreter,
    on_event: EventCallback | None = None,
) -> EngineResult:
    """起一个 agent 子进程，写 stdin、读 stream-json、等结束或超时回收，返回 ``EngineResult``。

    prompt 经 **stdin** 喂入（不走 argv 位置参数）以规避 OS 命令行参数上限：plan 暴涨时
    单个参数会顶爆 ARG_MAX（Linux 单参硬上限 128 KiB）让子进程根本起不来。stdin 是流、
    无此限制，上限抬到模型上下文窗口（撞线时是干净的模型层报错）。
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # 自成新会话/进程组：超时回收时可按进程组杀掉 agent 派生的孙进程（见
        # _terminate_process_tree）。POSIX 专用；项目目标即 macOS/Linux。
        start_new_session=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    events: list[dict] = []
    timed_out = False

    stdout_task = asyncio.create_task(
        _read_stream_json(proc.stdout, stream_log_path, events, on_event, interpreter)
    )
    stderr_task = asyncio.create_task(_read_stderr(proc.stderr))

    # prompt 经 stdin 写入。必须在 stdout reader task 已就绪后再写：大 prompt 写入时
    # 子进程会同时往 stdout 回吐，不并发 drain stdout 会双方互等死锁。_escape_leading_slash
    # 兜底仍保留——leading-`/` 在 stdin 模式下也可能被 CLI 当 slash 指令解析。
    try:
        proc.stdin.write(_escape_leading_slash(stdin_text).encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        # 子进程在读完 stdin 前已退出（如 flag 错误）；失败根因由 exit_code/stderr 兜住
        logger.warning("agent stdin 写入中断，子进程可能已提前退出")

    try:
        exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        timed_out = True
        logger.warning("agent 超时（%ss），SIGTERM 杀进程组", timeout_sec)
        _terminate_process_tree(proc, signal.SIGTERM)
        try:
            exit_code = await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("SIGTERM 后仍未退出，SIGKILL 杀进程组")
            _terminate_process_tree(proc, signal.SIGKILL)
            exit_code = await proc.wait()

    text_output, init_session_id = await stdout_task
    stderr_tail = await stderr_task

    result_is_error, error_message = interpreter.terminal_error(events)

    return EngineResult(
        exit_code=exit_code,
        text_output=text_output,
        init_session_id=init_session_id,
        stderr_tail=stderr_tail,
        timed_out=timed_out,
        events=events,
        result_is_error=result_is_error,
        error_message=error_message,
    )
