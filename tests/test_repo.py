"""repo.py 子命令超时回归。

聚焦本次修复：网络型 git / ssh 子命令挂起时必须超时抛 GitError 并回收子进程，
而不是永久阻塞 session task、占住并发槽。更全面的 repo.py 单测在后续 test PR 补。
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest

from cc_fleet.core import repo


class _HangingProc:
    """communicate() 永不返回的子进程替身，用来触发超时分支。"""

    def __init__(self, pid: int = 999_999):
        self.pid = pid
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    async def communicate(self):
        await asyncio.sleep(3600)  # 永不返回
        return b"", b""

    async def wait(self):
        self.waited = True
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


async def test_run_git_timeout_kills_group_and_raises(monkeypatch: pytest.MonkeyPatch):
    """_run_git 超时：杀进程组、回收子进程，并抛带「超时」的 GitError。"""
    killed: dict = {}
    monkeypatch.setattr(os, "getpgid", lambda pid: 7777)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.__setitem__("call", (pgid, sig)))

    proc = _HangingProc()

    async def fake_exec(*_a, **_k):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(repo.GitError) as ei:
        await repo._run_git(Path("."), "fetch", "origin", "main", timeout=0.05)

    assert "超时" in str(ei.value)
    assert killed.get("call") == (7777, signal.SIGKILL)  # 杀了整个进程组
    assert proc.waited  # 回收了子进程，避免僵尸


async def test_has_commits_ahead_remote_timeout_raises(monkeypatch: pytest.MonkeyPatch):
    """has_commits_ahead_remote 的 SSH 调用挂起时同样超时抛 GitError（不静默当作无提交）。"""
    monkeypatch.setattr(os, "getpgid", lambda pid: 1)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(repo, "_DEFAULT_GIT_TIMEOUT_SEC", 0.05)

    proc = _HangingProc()

    async def fake_exec(*_a, **_k):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(repo.GitError) as ei:
        await repo.has_commits_ahead_remote("devbox", "/remote/wt", "origin/main")

    assert "超时" in str(ei.value)
    assert proc.waited


async def test_run_git_no_timeout_when_fast(monkeypatch: pytest.MonkeyPatch):
    """正常快速返回时不受超时影响，原样透出 rc / stdout / stderr。"""

    class _FastProc:
        pid = 1
        returncode = 0

        async def communicate(self):
            return b"out\n", b""

    async def fake_exec(*_a, **_k):
        return _FastProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    rc, out, err = await repo._run_git(Path("."), "rev-parse", "HEAD", timeout=5)
    assert rc == 0
    assert out.strip() == "out"
    assert err == ""
