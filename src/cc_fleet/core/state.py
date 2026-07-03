"""Session 状态枚举与划分。

四种语义集合（注意 ``is_open`` 与 ``is_terminal`` 不再互补）：

- ``WORKING_STATES``：有子进程/任务在跑（NEW / PLANNING / PLAN_REVIEWING /
  DEVELOPING / CODE_REVIEWING / MR_SUBMITTING）。其中 PLAN_REVIEWING / CODE_REVIEWING
  是独立 Reviewer 在跑（仅启用 reviewer 的 repo 会经过）。
- ``{AWAITING_USER_CLARIFICATION}``：plan 阶段反问用户、等待澄清。
- ``RESUMABLE_TERMINAL_STATES``：状态机当前一轮已结，但用户引用 bot 回执回复可以
  唤醒进入下一轮（FAILED / TIMEOUT / COMPLETED）。
- ``{CANCELLED}``：用户已明确放弃，引用回复按"发新需求"对待。

派生谓词：

- ``is_working``：仅 WORKING_STATES。
- ``is_open``：WORKING ∪ AWAITING ∪ RESUMABLE_TERMINAL —— "能否接收用户消息继续推进"。
- ``is_terminal``：所有终态（FAILED / TIMEOUT / COMPLETED / CANCELLED）—— "状态机
  本轮是否已结"。

RESUMABLE_TERMINAL 同时满足 ``is_open=True`` 与 ``is_terminal=True``。CANCELLED 是唯一
``is_open=False`` 的终态。
"""

from __future__ import annotations

from enum import Enum


class SessionState(str, Enum):
    NEW = "new"
    PLANNING = "planning"
    AWAITING_USER_CLARIFICATION = "awaiting_user_clarification"
    # 独立 Reviewer 审查 plan / 代码的中间态（仅启用 reviewer 的 repo 会经过）。
    # 二者都有 Reviewer 子进程在跑，故归入 WORKING_STATES。
    PLAN_REVIEWING = "plan_reviewing"
    DEVELOPING = "developing"
    CODE_REVIEWING = "code_reviewing"
    MR_SUBMITTING = "mr_submitting"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    # `/chat` 自由对话通道的两个状态（独立于交付流水线，见 core/chat.py）：
    # CHATTING = 一轮 claude 正在跑；CHAT_AWAITING = 本轮已回发、等用户引用回复续聊。
    # 二者都属于 is_open（可被引用回复唤醒），但不进入 Session.drive 的流水线分派。
    CHATTING = "chatting"
    CHAT_AWAITING = "chat_awaiting"


WORKING_STATES: set[SessionState] = {
    SessionState.NEW,
    SessionState.PLANNING,
    SessionState.PLAN_REVIEWING,
    SessionState.DEVELOPING,
    SessionState.CODE_REVIEWING,
    SessionState.MR_SUBMITTING,
}


# 已结案但允许"引用回复唤醒"继续推进的终态。
RESUMABLE_TERMINAL_STATES: set[SessionState] = {
    SessionState.FAILED,
    SessionState.TIMEOUT,
    SessionState.COMPLETED,
}


TERMINAL_STATES: set[SessionState] = RESUMABLE_TERMINAL_STATES | {SessionState.CANCELLED}


# `/chat` 通道的 open 状态（is_open=True → 引用回复走 CONTINUE）。chat 的终态复用
# 现有 CANCELLED（/cancel）与 FAILED（子进程报错），不新增终态。
CHAT_STATES: set[SessionState] = {SessionState.CHATTING, SessionState.CHAT_AWAITING}


OPEN_STATES: set[SessionState] = (
    WORKING_STATES
    | {SessionState.AWAITING_USER_CLARIFICATION}
    | RESUMABLE_TERMINAL_STATES
    | CHAT_STATES
)


def is_terminal(state: SessionState | str) -> bool:
    s = SessionState(state) if isinstance(state, str) else state
    return s in TERMINAL_STATES


def is_working(state: SessionState | str) -> bool:
    s = SessionState(state) if isinstance(state, str) else state
    return s in WORKING_STATES


def is_open(state: SessionState | str) -> bool:
    s = SessionState(state) if isinstance(state, str) else state
    return s in OPEN_STATES


def is_resumable_terminal(state: SessionState | str) -> bool:
    """是否属于"已结案但可唤醒"终态。CANCELLED 不算。"""
    s = SessionState(state) if isinstance(state, str) else state
    return s in RESUMABLE_TERMINAL_STATES
