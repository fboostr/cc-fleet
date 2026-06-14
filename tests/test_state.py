"""state.py 的状态划分不变式测试。

确保：
- 四个集合（WORKING / {AWAITING} / RESUMABLE_TERMINAL / {CANCELLED}）互不相交且
  覆盖 SessionState 全集。
- ``is_open`` = WORKING ∪ {AWAITING} ∪ RESUMABLE_TERMINAL —— "能否接收用户消息推进"。
- ``is_terminal`` = RESUMABLE_TERMINAL ∪ {CANCELLED} —— "状态机本轮是否已结"。
- RESUMABLE_TERMINAL 同时满足 ``is_open=True`` 与 ``is_terminal=True``，这是引用回复
  能唤醒 FAILED/TIMEOUT/COMPLETED session 的语义基础（之前 is_open/is_terminal
  互补时这种 session 被错判为"非活跃"，回执引用回复直接开新 session 丢上下文）。
- CANCELLED 是唯一 ``is_open=False`` 的终态。
"""

from __future__ import annotations

from cc_fleet.core.state import (
    OPEN_STATES,
    RESUMABLE_TERMINAL_STATES,
    TERMINAL_STATES,
    WORKING_STATES,
    SessionState,
    is_open,
    is_resumable_terminal,
    is_terminal,
    is_working,
)


def test_four_partitions_cover_all_states_and_disjoint():
    all_states = set(SessionState)
    awaiting = {SessionState.AWAITING_USER_CLARIFICATION}
    cancelled = {SessionState.CANCELLED}
    assert WORKING_STATES | awaiting | RESUMABLE_TERMINAL_STATES | cancelled == all_states
    assert WORKING_STATES.isdisjoint(awaiting)
    assert WORKING_STATES.isdisjoint(RESUMABLE_TERMINAL_STATES)
    assert WORKING_STATES.isdisjoint(cancelled)
    assert awaiting.isdisjoint(RESUMABLE_TERMINAL_STATES)
    assert awaiting.isdisjoint(cancelled)
    assert RESUMABLE_TERMINAL_STATES.isdisjoint(cancelled)


def test_resumable_terminal_members():
    assert RESUMABLE_TERMINAL_STATES == {
        SessionState.FAILED,
        SessionState.TIMEOUT,
        SessionState.COMPLETED,
    }


def test_terminal_is_resumable_plus_cancelled():
    assert TERMINAL_STATES == RESUMABLE_TERMINAL_STATES | {SessionState.CANCELLED}


def test_open_is_working_plus_awaiting_plus_resumable():
    assert OPEN_STATES == (
        WORKING_STATES
        | {SessionState.AWAITING_USER_CLARIFICATION}
        | RESUMABLE_TERMINAL_STATES
    )
    # is_open 与 is_terminal 不再互补：RESUMABLE_TERMINAL 两者皆为 True
    assert OPEN_STATES & TERMINAL_STATES == RESUMABLE_TERMINAL_STATES


def test_awaiting_is_open_but_not_working_not_terminal():
    s = SessionState.AWAITING_USER_CLARIFICATION
    assert is_open(s) is True
    assert is_working(s) is False
    assert is_terminal(s) is False


def test_resumable_terminal_is_open_and_terminal():
    for s in (SessionState.FAILED, SessionState.TIMEOUT, SessionState.COMPLETED):
        assert is_open(s) is True
        assert is_terminal(s) is True
        assert is_resumable_terminal(s) is True
        assert is_working(s) is False


def test_cancelled_is_terminal_not_open():
    s = SessionState.CANCELLED
    assert is_open(s) is False
    assert is_terminal(s) is True
    assert is_resumable_terminal(s) is False
    assert is_working(s) is False


def test_predicates_accept_string():
    assert is_open("awaiting_user_clarification") is True
    assert is_working("planning") is True
    assert is_terminal("completed") is True
    assert is_open("completed") is True       # 改动后 completed 也算 open
    assert is_open("cancelled") is False
    assert is_resumable_terminal("failed") is True
    assert is_resumable_terminal("cancelled") is False


def test_every_state_has_some_classification():
    # 每个 state 至少属于 is_open 或 is_terminal（CANCELLED 只 terminal，
    # working/awaiting 只 open，resumable 两者皆是）
    for s in SessionState:
        assert is_open(s) or is_terminal(s)
