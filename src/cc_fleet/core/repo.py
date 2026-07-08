"""git worktree 操作封装。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import signal
from pathlib import Path

logger = logging.getLogger(__name__)

# git / ssh 子命令默认超时。网络型调用（fetch origin、ssh rev-list）若因远端不可达 /
# 网络抖动挂起，不设防会让 session task 永久阻塞在 communicate() 上，一直占着并发槽
# （_session_loop 在 async with semaphore 内），几个卡死就把 max_concurrent_sessions
# 耗尽且无超时/无 _fail/无通知。本地 git 操作远快于此，不会误伤。
_DEFAULT_GIT_TIMEOUT_SEC = 120.0


class GitError(RuntimeError):
    """git 子命令失败。"""


def _kill_proc_tree(proc: asyncio.subprocess.Process) -> None:
    """杀掉子进程所在进程组（git fetch 会派生 ssh 等孙进程）。POSIX 专用，拿不到进程组
    时退回只杀直接子进程；已退出则跳过。"""
    if proc.returncode is not None:
        return
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    if killpg is not None and getpgid is not None:
        try:
            killpg(getpgid(proc.pid), signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def _communicate_or_timeout(
    proc: asyncio.subprocess.Process, timeout: float, what: str
) -> tuple[bytes, bytes]:
    """等子进程结束并收集输出；超时则杀进程组、回收，并抛 GitError。"""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        _kill_proc_tree(proc)
        with contextlib.suppress(Exception):
            await proc.wait()
        raise GitError(f"{what} 超时（>{timeout:g}s 未返回），已终止子进程") from e


async def _run_git(
    cwd: Path, *args: str, timeout: float = _DEFAULT_GIT_TIMEOUT_SEC
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out_b, err_b = await _communicate_or_timeout(proc, timeout, f"git {args[0] if args else ''}")
    return proc.returncode or 0, out_b.decode("utf-8", errors="replace"), err_b.decode("utf-8", errors="replace")


async def fetch_default_branch(
    repo_root: Path, default_branch: str, remote: str = "origin"
) -> None:
    rc, _, err = await _run_git(repo_root, "fetch", remote, default_branch)
    if rc != 0:
        raise GitError(f"git fetch {remote} {default_branch} 失败：{err.strip()}")


async def create_worktree(
    repo_root: Path,
    worktree_path: Path,
    branch: str,
    base: str = "origin/main",
) -> None:
    """基于 base（形如 {base_remote}/<default_branch>）起新分支并加 worktree。"""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = await _run_git(
        repo_root, "worktree", "add", "-b", branch, str(worktree_path), base
    )
    if rc != 0:
        raise GitError(f"git worktree add 失败：{err.strip()}")
    logger.info("worktree 创建：%s（分支 %s，基于 %s）", worktree_path, branch, base)


async def create_detached_worktree(repo_root: Path, worktree_path: Path, ref: str) -> None:
    """基于 ref 建一个 detached（无分支）worktree，用于只读浏览最新代码（如 /chat 只读讨论）。

    detached 而非 `-b <branch>`：只读复用、不占分支命名空间，也不与 dev 的 `claude/<slug>` 撞。
    """
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = await _run_git(
        repo_root, "worktree", "add", "--detach", str(worktree_path), ref
    )
    if rc != 0:
        raise GitError(f"git worktree add --detach 失败：{err.strip()}")
    logger.info("只读 worktree 创建：%s（基于 %s）", worktree_path, ref)


async def sync_detached_worktree(worktree_path: Path, ref: str) -> None:
    """把已有 detached worktree 硬同步到 ref 的最新提交（`--force` 丢弃任何工作树改动）。

    共享只读 chat worktree 在每次新对话开场时据此推进到最新 base（含刚合并的功能）。
    """
    rc, _, err = await _run_git(
        worktree_path, "checkout", "--detach", "--force", ref
    )
    if rc != 0:
        raise GitError(f"git checkout --detach --force {ref} 失败：{err.strip()}")
    logger.info("只读 worktree 同步：%s → %s", worktree_path, ref)


async def remove_worktree(repo_root: Path, worktree_path: Path, force: bool = False) -> None:
    args = ["worktree", "remove", str(worktree_path)]
    if force:
        args.insert(2, "--force")
    rc, _, err = await _run_git(repo_root, *args)
    if rc != 0:
        # 即便失败也尽量清理目录，避免悬挂
        logger.warning("git worktree remove 失败（rc=%s）：%s", rc, err.strip())
        raise GitError(err.strip() or "worktree remove failed")
    logger.info("worktree 移除：%s", worktree_path)


async def has_commits_ahead(worktree_path: Path, base_ref: str) -> bool:
    """worktree 当前 HEAD 相对 base_ref 是否有新提交。"""
    rc, out, _ = await _run_git(
        worktree_path, "rev-list", "--count", f"{base_ref}..HEAD"
    )
    if rc != 0:
        return False
    return int(out.strip() or "0") > 0


async def has_commits_ahead_remote(
    ssh_alias: str, remote_worktree: str, base_ref: str
) -> bool:
    """远端 worktree 当前 HEAD 相对 base_ref 是否有新提交（经 SSH 查，remote 模式用）。

    与 ``has_commits_ahead`` 对仗，只是把 ``git rev-list --count`` 经 ``ssh <alias>``
    放到远端 worktree 里跑——defer-push 下主控不在本地持有代码，需用它当「dev 是否
    已 commit」的 ground-truth 闸门（对齐 local 的 ``has_commits_ahead``）。

    与 local 版的差别：SSH / git 任一失败（连不上、目录不存在、rc!=0、输出非数字）
    一律抛 ``GitError``，由调用方判失败并给清晰错误——**不静默当作「无提交」**，避免把
    「查不到」误判成「没写代码」。
    """
    # remote_worktree / base_ref 来自主控配置或确定性拼接（非用户自由输入），仍用
    # shlex.quote 兜底，防路径中的特殊字符破坏远端 shell 解析。
    inner = (
        f"cd {shlex.quote(remote_worktree)} && "
        f"git rev-list --count {shlex.quote(f'{base_ref}..HEAD')}"
    )
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        ssh_alias,
        inner,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out_b, err_b = await _communicate_or_timeout(
        proc, _DEFAULT_GIT_TIMEOUT_SEC, f"ssh {ssh_alias} 查远端提交数"
    )
    rc = proc.returncode or 0
    out = out_b.decode("utf-8", errors="replace").strip()
    err = err_b.decode("utf-8", errors="replace").strip()
    if rc != 0:
        raise GitError(f"ssh {ssh_alias} 查远端提交数失败（rc={rc}）：{err or out}")
    try:
        return int(out or "0") > 0
    except ValueError as e:
        raise GitError(f"ssh {ssh_alias} 远端提交数输出非预期：{out!r}") from e


async def has_uncommitted_changes(worktree_path: Path) -> bool:
    """worktree 是否有未提交改动（已改未 commit / 已 add 未 commit / 未跟踪新文件）。

    用 ``git status --porcelain``：输出非空即 dirty。**仅**用于 commit 闸门失败时区分
    「claude 没写代码」与「写了没 commit」以给准确文案，故与 ``has_commits_ahead`` 一样对
    git 失败**容错**（rc!=0 时返回 False，退化到泛化文案），不因这条诊断查询本身出错盖住主失败。
    """
    rc, out, _ = await _run_git(worktree_path, "status", "--porcelain")
    if rc != 0:
        return False
    return bool(out.strip())


async def has_uncommitted_changes_remote(ssh_alias: str, remote_worktree: str) -> bool:
    """远端 worktree 是否有未提交改动（经 SSH 查 ``git status --porcelain``，remote 模式用）。

    与 ``has_uncommitted_changes`` 对仗、与 ``has_commits_ahead_remote`` 同构：SSH/git 失败
    抛 ``GitError``（不静默），由调用方决定是否降级。仅用于 commit 闸门失败时细化文案。
    """
    inner = f"cd {shlex.quote(remote_worktree)} && git status --porcelain"
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        ssh_alias,
        inner,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out_b, err_b = await _communicate_or_timeout(
        proc, _DEFAULT_GIT_TIMEOUT_SEC, f"ssh {ssh_alias} 查远端工作区状态"
    )
    rc = proc.returncode or 0
    out = out_b.decode("utf-8", errors="replace").strip()
    err = err_b.decode("utf-8", errors="replace").strip()
    if rc != 0:
        raise GitError(f"ssh {ssh_alias} 查远端工作区状态失败（rc={rc}）：{err or out}")
    return bool(out)


async def ensure_remote_chat_worktree(
    ssh_alias: str,
    remote_repo_path: str,
    chat_worktree_path: str,
    base_remote: str,
    default_branch: str,
) -> None:
    """经 SSH 在远端 fetch base 分支并建/同步只读 chat worktree（主控发起的受控写，remote 模式用）。

    远端已存在该 worktree（含 `.git`）→ `checkout --detach --force` 同步到最新；否则
    `worktree add --detach` 新建。随后 /chat 阶段 claude 经 ssh **只读**读取它（见
    chat_protocol_remote.md）。参数均来自主控配置或确定性拼接，仍 shlex.quote 兜底。
    """
    ref = f"{base_remote}/{default_branch}"
    git_marker = chat_worktree_path.rstrip("/") + "/.git"
    inner = (
        f"cd {shlex.quote(remote_repo_path)} && "
        f"git fetch {shlex.quote(base_remote)} {shlex.quote(default_branch)} && "
        f"if [ -e {shlex.quote(git_marker)} ]; then "
        f"git -C {shlex.quote(chat_worktree_path)} checkout --detach --force {shlex.quote(ref)}; "
        f"else "
        f"git worktree add --detach {shlex.quote(chat_worktree_path)} {shlex.quote(ref)}; "
        f"fi"
    )
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        ssh_alias,
        inner,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out_b, err_b = await _communicate_or_timeout(
        proc, _DEFAULT_GIT_TIMEOUT_SEC, f"ssh {ssh_alias} 建/同步 chat worktree"
    )
    rc = proc.returncode or 0
    if rc != 0:
        err = err_b.decode("utf-8", errors="replace").strip()
        out = out_b.decode("utf-8", errors="replace").strip()
        raise GitError(
            f"ssh {ssh_alias} 建/同步远端 chat worktree 失败（rc={rc}）：{err or out}"
        )


async def get_commits_ahead_subjects(
    worktree_path: Path, base_ref: str
) -> list[str]:
    """返回 worktree 相对 base_ref 的所有新提交 subject（按时间倒序，最近的在前）。

    失败或没有新提交时返回空列表，调用方自行兜底。仅用于 MR 标题/描述兜底，
    不参与状态判断，因此对失败容忍——不抛 GitError。
    """
    rc, out, _ = await _run_git(
        worktree_path, "log", f"{base_ref}..HEAD", "--pretty=%s"
    )
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


async def current_branch(worktree_path: Path) -> str:
    rc, out, err = await _run_git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        raise GitError(err.strip() or "rev-parse failed")
    return out.strip()
