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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .base import EventCallback, TimeoutKind, TimeoutPolicy

logger = logging.getLogger(__name__)


class StreamInterpreter(Protocol):
    """把一条已解析的 stream 事件翻译成增量文本 / session_id / 终态错误。逐工具实现。"""

    def consume(self, evt: dict, text_parts: list[str]) -> None:
        """按本工具规则把 evt 的文本增量原地追加到 ``text_parts``（含各自的去重策略）。"""

    def session_id(self, evt: dict) -> str | None:
        """从 evt 抽取会话 id（如 claude 的 system/init 事件）；无则 ``None``。"""

    def terminal_error(self, events: list[dict]) -> tuple[bool, str | None]:
        """从事件流抽取终态失败标记与人读错误文本。"""

    def tool_activity(self, evt: dict) -> list[tuple[str, str]]:
        """从 evt 抽取工具生命周期信号，供引擎的空闲监控区分「有工具在飞」与「回合间空闲」。

        返回列表，每项 ``("start", tool_id)`` 表示一次工具调用发起、``("end", tool_id)``
        表示其结果已回；一条事件可含多个（并行工具调用 / 批量结果），无则空列表。逐工具
        实现（claude 见 ``ClaudeInterpreter``）；未实现本方法的 interpreter 由引擎用 getattr
        兜底，退化为「只按 idle 档计时」——功能降级但不出错。
        """


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
    # 超时的具体档位（idle / tool / hard_cap），未超时为 None；``killed`` 表示被外部
    # ``kill_event`` 手动强杀（非超时）。语义见 ``base.TimeoutPolicy`` 与本文件监控循环。
    timeout_kind: TimeoutKind | None = None
    killed: bool = False


class _ActivityTracker:
    """跟踪 stream 的「最近活动时刻」与「是否有工具在飞」，供监控循环判空闲。

    - ``last_event_at``：每解析出一条事件就刷新（单调时钟）——只要 agent 还在吐东西就算活着。
    - ``tool_in_flight``：``tool_use`` 已发但对应 ``tool_result`` 未回时为真。据此让监控循环
      在「有工具在跑」时放宽静默上限（长编译 / 测试期间进程可能整段静默），避免误杀。

    工具生命周期识别下沉到 ``interpreter.tool_activity``（工具耦合只在 interpreter 层）；
    interpreter 未实现该方法时退化为「只更新 last_event_at、tool_in_flight 恒 False」。
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.last_event_at = clock()
        self._pending: set[str] = set()

    def note_event(self, evt: dict, interpreter: StreamInterpreter) -> None:
        self.last_event_at = self._clock()
        fn = getattr(interpreter, "tool_activity", None)
        if fn is None:
            return
        for kind, tool_id in fn(evt) or ():
            if kind == "start":
                self._pending.add(tool_id)
            elif kind == "end":
                self._pending.discard(tool_id)

    @property
    def tool_in_flight(self) -> bool:
        return bool(self._pending)


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
    activity: _ActivityTracker | None = None,
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

    # 刷新「最近活动时刻」+ 维护「工具在飞」状态，供监控循环判空闲。放在解析成功后、
    # on_event 之前：即便下游回调异常也不影响活性计时。
    if activity is not None:
        activity.note_event(evt, interpreter)

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
    activity: _ActivityTracker | None = None,
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
                        bytes(buf), f, text_parts, events, detected_session_id, on_event, interpreter, activity
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
                    line, f, text_parts, events, detected_session_id, on_event, interpreter, activity
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


# 监控循环的轮询周期：每隔这么久醒一次，检查空闲 / 绝对上限 / 手动终止。
# 1s 相对分钟级阈值足够灵敏，CPU 开销可忽略。
_POLL_INTERVAL_SEC = 1.0


def _overrun(
    *,
    now: float,
    start: float,
    last_event_at: float,
    in_flight: bool,
    timeout: TimeoutPolicy,
) -> TimeoutKind | None:
    """纯判定：给定当前/起始/最近活动时刻与「是否有工具在飞」，返回触发的超时档，未超时为 None。

    从监控循环抽出的纯函数（不碰进程 / IO / 时钟），便于穷举单测三档边界：
    - ``hard_cap``：从进程启动起算总时长超 ``hard_cap_sec``，**优先级最高**（绝对兜底）。
    - ``tool`` / ``idle``：静默（``now - last_event_at``）超过对应档上限——有工具在飞用宽松的
      ``tool_sec``、否则用收紧的 ``idle_sec``。这正是「长编译/测试不误杀、真卡死快回收」。
    """
    if now - start > timeout.hard_cap_sec:
        return "hard_cap"
    idle = now - last_event_at
    limit = timeout.tool_sec if in_flight else timeout.idle_sec
    if idle > limit:
        return "tool" if in_flight else "idle"
    return None


async def _kill_and_reap(
    proc: asyncio.subprocess.Process, wait_task: "asyncio.Future[int | None]"
) -> int | None:
    """两段式回收进程组：先 SIGTERM，5s 内未退再 SIGKILL，返回最终退出码。

    ``wait_task`` 是监控循环常驻的 ``proc.wait()`` future——复用同一个而非另起新等待，
    杀完在此收尸。
    """
    _terminate_process_tree(proc, signal.SIGTERM)
    done, _ = await asyncio.wait({wait_task}, timeout=5)
    if wait_task not in done:
        logger.warning("SIGTERM 后仍未退出，SIGKILL 杀进程组")
        _terminate_process_tree(proc, signal.SIGKILL)
        await wait_task
    return wait_task.result()


async def run_subprocess(
    *,
    argv: list[str],
    cwd: Path,
    stdin_text: str,
    env: dict[str, str],
    timeout: TimeoutPolicy,
    stream_log_path: Path,
    interpreter: StreamInterpreter,
    on_event: EventCallback | None = None,
    kill_event: asyncio.Event | None = None,
) -> EngineResult:
    """起一个 agent 子进程，写 stdin、读 stream-json，按空闲策略监控回收，返回 ``EngineResult``。

    prompt 经 **stdin** 喂入（不走 argv 位置参数）以规避 OS 命令行参数上限：plan 暴涨时
    单个参数会顶爆 ARG_MAX（Linux 单参硬上限 128 KiB）让子进程根本起不来。stdin 是流、
    无此限制，上限抬到模型上下文窗口（撞线时是干净的模型层报错）。

    回收不再按「进程总时长」一刀切，而是每 ``_POLL_INTERVAL_SEC`` 醒一次做三档判定
    （见 ``TimeoutPolicy``）：无工具在飞时静默超 ``idle_sec``、有工具在飞时静默超
    ``tool_sec``、总时长超 ``hard_cap_sec``，任一触发即杀进程组并置 ``timeout_kind``。
    另轮询 ``kill_event``：被外部 set 时立即强杀并置 ``killed=True``（``/kill`` 用）。
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

    loop = asyncio.get_running_loop()
    activity = _ActivityTracker(loop.time)
    events: list[dict] = []

    stdout_task = asyncio.create_task(
        _read_stream_json(proc.stdout, stream_log_path, events, on_event, interpreter, activity)
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

    # 监控循环：常驻一个 proc.wait() future，每 _POLL_INTERVAL_SEC 醒来检查空闲 / 绝对
    # 上限 / 手动终止。用 asyncio.wait(timeout=...) 而非 wait_for——超时只唤醒本轮、不取消
    # wait_task，下轮复用同一个等待。
    timeout_kind: TimeoutKind | None = None
    killed = False
    start = loop.time()
    wait_task: asyncio.Future[int | None] = asyncio.ensure_future(proc.wait())
    while True:
        done, _ = await asyncio.wait({wait_task}, timeout=_POLL_INTERVAL_SEC)
        if wait_task in done:
            exit_code = wait_task.result()  # 进程自然退出
            break
        if kill_event is not None and kill_event.is_set():
            killed = True
            logger.warning("收到手动终止（kill_event），SIGTERM 杀进程组")
            exit_code = await _kill_and_reap(proc, wait_task)
            break
        timeout_kind = _overrun(
            now=loop.time(),
            start=start,
            last_event_at=activity.last_event_at,
            in_flight=activity.tool_in_flight,
            timeout=timeout,
        )
        if timeout_kind is not None:
            logger.warning(
                "agent 被回收（%s 档超时），SIGTERM 杀进程组；idle/tool/hard_cap=%s/%s/%s",
                timeout_kind,
                timeout.idle_sec,
                timeout.tool_sec,
                timeout.hard_cap_sec,
            )
            exit_code = await _kill_and_reap(proc, wait_task)
            break

    timed_out = timeout_kind is not None
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
        timeout_kind=timeout_kind,
        killed=killed,
    )
