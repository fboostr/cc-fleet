"""PreToolUse hook 的关键拦截行为。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import pytest

from cc_fleet.security.hooks import pretool_guard


@pytest.fixture
def worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wt = (tmp_path / "wt").resolve()
    wt.mkdir()
    monkeypatch.setenv("CC_FLEET_WORKTREE", str(wt))
    monkeypatch.delenv("CC_FLEET_EXTRA_WORKTREE_ROOTS", raising=False)
    return wt


class RemoteEnv(NamedTuple):
    """模拟远端模式：本地壳子目录 + 远端项目根 + 远端 worktree 根。

    测试里都用本地 tmp 目录代替远端真实路径，便于走 Path.resolve()/exists() 而不
    依赖网络与远端机器。
    """

    local_shell: Path
    remote_repo: Path
    remote_wt_root: Path


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RemoteEnv:
    local_shell = (tmp_path / "local-shell").resolve()
    local_shell.mkdir()
    remote_repo = (tmp_path / "remote" / "repo").resolve()
    remote_repo.mkdir(parents=True)
    remote_wt_root = (tmp_path / "remote" / "wt-root").resolve()
    remote_wt_root.mkdir(parents=True)

    monkeypatch.setenv("CC_FLEET_WORKTREE", str(local_shell))
    monkeypatch.setenv(
        "CC_FLEET_EXTRA_WORKTREE_ROOTS",
        os.pathsep.join([str(remote_repo), str(remote_wt_root)]),
    )
    return RemoteEnv(local_shell, remote_repo, remote_wt_root)


# --- force push 各变体 ---

@pytest.mark.parametrize(
    "command",
    [
        "git push --force origin HEAD",
        "git push origin main --force",
        "git push -f origin HEAD",
        "git push --force-with-lease",
        "git push origin +main:main",                # refspec 加号前缀
        "git -c user.name=foo push --force",         # 带 -c 选项
        "git push --force-with-lease=origin/main",
    ],
)
def test_force_push_blocked(command: str, worktree: Path):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None, f"应拦截：{command}"
    assert "force" in reason.lower() or "禁止" in reason


def test_normal_push_allowed(worktree: Path):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push origin feature/x"}}
    assert pretool_guard.evaluate(payload) is None


# --- 敏感目录 ---

@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.ssh/id_rsa",
        "echo x > ~/.ssh/authorized_keys",
        "cat /etc/passwd",
        "ls /.ssh/",
    ],
)
def test_sensitive_path_blocked(command: str, worktree: Path):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None


# --- Bash 写操作 + worktree 外路径 ---

def test_bash_write_outside_worktree_blocked(worktree: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"echo hi > {outside}"},
    }
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


def test_bash_write_inside_worktree_allowed(worktree: Path):
    inside = worktree / "new.txt"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"echo hi > {inside}"},
    }
    assert pretool_guard.evaluate(payload) is None


def test_bash_read_command_allowed(worktree: Path):
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}}
    # 只读命令不应被拦
    assert pretool_guard.evaluate(payload) is None


# --- heredoc 正文里的 `/xxx` 字面量不应被当成越界路径 ---
# 修复点：旧版守卫在 WRITE_TOKENS 命中后，会对整条命令（含 heredoc body）扫绝对路径
# 正则，导致 yaml 注释里的 `/healthcheck` 等字面量被误拦。

def test_heredoc_body_slash_token_not_blocked(worktree: Path):
    target = worktree / "config.yaml"
    cmd = (
        f"tee {target} <<'EOF'\n"
        "# 远端服务稳定性自检 probe 配置\n"
        "# 触发方式：在企微对话里发 /healthcheck（隐藏指令，面向服务维护者）。\n"
        "EOF\n"
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


def test_heredoc_body_does_not_mask_outside_redirect(worktree: Path):
    # heredoc 正文外仍有越界 redirect → 必须被拦
    cmd = (
        "cat <<'EOF' > /tmp/should-not-write\n"
        "harmless body line\n"
        "EOF\n"
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


def test_dash_heredoc_with_tab_indented_end_supported(worktree: Path):
    # <<-EOF 形式允许结束行带 tab 缩进；body 里的 /xxx 不应误拦
    target = worktree / "out.txt"
    cmd = (
        f"\ttee {target} <<-EOF\n"
        "\t参考链接：/some/external/path\n"
        "\tEOF\n"
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


# --- single/double-quoted 字面量里的 `/xxx` 不应被当成越界路径 ---
# 修复点：MR #39 之后仍有同类误拦——`git push -o "merge_request.description=…
# /healthcheck…" 2>&1` 这种 push option value 里的 slash command 被路径正则
# 扫到。守卫现在在路径扫描前再剥一道引号 + fd-dup。

def test_quoted_slash_token_in_push_option_not_blocked(worktree: Path):
    # 本地直 push（不含 ssh），double-quote 包裹 push option value
    cmd = (
        'git push -u origin HEAD '
        '-o merge_request.create '
        '-o "merge_request.title=展示 probe 关键参数" '
        '-o "merge_request.description=## 背景\\n\\n/healthcheck 输出的表格 …" '
        '-o merge_request.remove_source_branch '
        '2>&1'
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


def test_single_quoted_slash_token_not_blocked(worktree: Path):
    # 单引号包裹（远端 ssh 命令外层 double-quote、内层 push option 用 single）
    cmd = (
        "git push -u origin HEAD "
        "-o 'merge_request.description=foo /healthcheck /help bar' "
        "2>&1"
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


def test_fd_dup_alone_does_not_trigger_write_scan(worktree: Path):
    # 命令里只有 `2>&1` 这种 fd-dup、没有真 redirect / 写命令；命令参数里出现
    # 越界路径不应被拦（参数本身只是读路径）
    cmd = "ls -la /etc/hosts 2>&1"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


def test_real_unquoted_redirect_still_blocks(worktree: Path):
    # 剥引号 + 剥 fd-dup 后，引号外的真 redirect 目标仍可见 → 必须被拦
    cmd = 'echo "harmless body" > /tmp/should-not-write 2>&1'
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


# --- 重定向到 /dev/null 等安全伪设备不应被拦 ---
# 修复点：`2>/dev/null` 里的 `>` 会命中 WRITE_TOKENS，`/dev/null` 又被绝对路径正则抠出，
# 旧版据此误判"工作目录外写入"。守卫现在豁免 /dev/null、/dev/std*、/dev/tty、/dev/fd/<n>
# 等安全伪设备（写入无副作用），但不豁免 /dev/sda 这类真实块设备。

@pytest.mark.parametrize(
    "command",
    [
        # 用户实际报错的命令原型
        'grep -rn "用法" src/ 2>/dev/null; echo "---"; grep -rln "/plan" src/ 2>/dev/null',
        "echo hi > /dev/null",
        "some-cmd 2>/dev/null",
        "some-cmd &>/dev/null",
        "some-cmd >/dev/null 2>&1",
        "some-cmd 2>/dev/stderr",
        "some-cmd >/dev/stdout",
        "diff <(sort a) /dev/fd/2",
    ],
)
def test_redirect_to_safe_device_allowed(command: str, worktree: Path):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    assert pretool_guard.evaluate(payload) is None, f"不应拦截：{command}"


def test_dd_to_real_block_device_still_blocked(worktree: Path):
    # /dev/zero（源）豁免，但 /dev/sda（真实块设备写目标）不豁免 → 必须被拦
    cmd = "dd if=/dev/zero of=/dev/sda bs=1M"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


def test_safe_device_does_not_mask_other_outside_write(worktree: Path):
    # 同一命令内既写 /dev/null（豁免）又写真越界路径 → 越界那个仍要被拦
    cmd = "echo x > /dev/null; echo y > /tmp/should-not-write"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


def test_dev_null_path_traversal_still_blocked(worktree: Path):
    # 伪装成 /dev/null 前缀的路径穿越不应被豁免
    cmd = "echo x > /dev/null/../../../etc/cron.d/evil"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


def test_redirect_to_dev_null_allowed_in_remote_mode(remote: RemoteEnv):
    # 远端模式（注入了额外白名单根）下，本地探查命令的 2>/dev/null 同样应放行
    cmd = "grep -rn foo src/ 2>/dev/null"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


# --- Write / Edit 路径检查 ---

def test_write_inside_worktree_allowed(worktree: Path):
    target = worktree / "file.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": "x"}}
    assert pretool_guard.evaluate(payload) is None


def test_write_outside_worktree_blocked(worktree: Path, tmp_path: Path):
    outside = tmp_path / "outside.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(outside), "content": "x"}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


def test_edit_outside_worktree_blocked(worktree: Path, tmp_path: Path):
    outside = tmp_path / "outside.py"
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(outside), "old_string": "a", "new_string": "b"},
    }
    assert pretool_guard.evaluate(payload) is not None


def test_relative_path_allowed(worktree: Path):
    payload = {"tool_name": "Write", "tool_input": {"file_path": "relative.py", "content": "x"}}
    # 相对路径相对 cwd（worktree），通过
    assert pretool_guard.evaluate(payload) is None


# --- 其他工具不拦截 ---

def test_other_tools_pass(worktree: Path):
    payload = {"tool_name": "Read", "tool_input": {"file_path": "/anything"}}
    assert pretool_guard.evaluate(payload) is None


# --- 缺少 worktree 环境变量时的兜底 ---

def test_no_worktree_env_still_blocks_force_push(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CC_FLEET_WORKTREE", raising=False)
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}
    assert pretool_guard.evaluate(payload) is not None


def test_no_worktree_env_blocks_absolute_write(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CC_FLEET_WORKTREE", raising=False)
    monkeypatch.delenv("CC_FLEET_EXTRA_WORKTREE_ROOTS", raising=False)
    payload = {"tool_name": "Write", "tool_input": {"file_path": "/etc/foo", "content": "x"}}
    assert pretool_guard.evaluate(payload) is not None


# --- 远端模式：扩展白名单（CC_FLEET_EXTRA_WORKTREE_ROOTS） ---
# 修复点：远端模式下 cwd 是本地壳子目录，但真正的写入发生在远端绝对路径上；
# hook 把远端 repo 根与远端 worktree 根纳入白名单后，含 `2>&1`/`ln`/`tee`
# 等 WRITE_TOKEN 的 ssh 命令不应再被误判成"工作目录外的写"。

def test_remote_ssh_command_with_redirect_allowed(remote: RemoteEnv):
    cmd = (
        f"ssh dev01.example.com 'ls -la {remote.remote_repo}/AGENTS.md 2>&1; "
        f"echo \"---\"; cat {remote.remote_repo}/AGENTS.md 2>&1 | head -200'"
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


def test_remote_worktree_create_command_allowed(remote: RemoteEnv):
    cmd = (
        f"ssh dev01.example.com 'set -e; cd {remote.remote_repo}; "
        f"git fetch origin master; "
        f"git worktree add {remote.remote_wt_root}/feat-x -b claude/feat-x origin/master; "
        f"cd {remote.remote_wt_root}/feat-x; "
        f"[ -e repos ] || ln -s {remote.remote_repo}/repos repos'"
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


def test_remote_mode_still_blocks_outside_paths(remote: RemoteEnv):
    # 即便注入了远端白名单，命令里出现真正越界的路径仍要被拦
    cmd = "echo evil > /tmp/should-not-write"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


def test_remote_mode_still_blocks_force_push(remote: RemoteEnv):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
    }
    assert pretool_guard.evaluate(payload) is not None


def test_remote_mode_still_blocks_sensitive(remote: RemoteEnv):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ssh dev01.example.com 'cat /etc/passwd'"},
    }
    assert pretool_guard.evaluate(payload) is not None


def test_remote_mode_write_to_remote_repo_allowed(remote: RemoteEnv):
    # Write 工具的 file_path 落在远端 repo 根下，应放行
    target = remote.remote_repo / "AGENTS.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "x"},
    }
    assert pretool_guard.evaluate(payload) is None


def test_remote_mode_quoted_push_option_with_slash_command_allowed(remote: RemoteEnv):
    # 复现用户当前报错形态：ssh 双引号包裹 + 内层 push option 用 single-quote +
    # description value 含 `/healthcheck` / `/help` + 末尾 `2>&1`
    cmd = (
        f'ssh dev01.example.com "cd {remote.remote_wt_root}/healthcheck-show-probe-target && '
        "git push -u origin HEAD "
        "-o merge_request.create "
        "-o merge_request.target=master "
        "-o 'merge_request.title=feat: 展示 probe 关键参数' "
        "-o 'merge_request.description=## 背景\\n\\n/healthcheck 输出表格扩展，列出 "
        "部署单元的 app 名、监控指标名。' "
        "-o merge_request.remove_source_branch "
        '2>&1"'
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


def test_remote_mode_heredoc_body_slash_token_allowed(remote: RemoteEnv):
    # 远端模式典型形态：ssh + cd + tee + heredoc 写远端 worktree 下的 yaml；
    # body 里的 `/healthcheck` 应被剥离，不应被路径正则拦截
    target = remote.remote_wt_root / "config.yaml"
    cmd = (
        f"ssh dev01.example.com 'cd {remote.remote_wt_root} && "
        f"tee {target} <<EOF\n"
        "# probe 配置\n"
        "# 触发方式：在企微里发 /healthcheck\n"
        "EOF\n"
        "'"
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert pretool_guard.evaluate(payload) is None


def test_extra_roots_empty_pieces_ignored(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
):
    # 配置侧给到的 extra 字符串有时含空段（如 remote_worktree_root 未配），
    # 不应让 hook 把空字符串解析成根 "/"。
    monkeypatch.setenv("CC_FLEET_EXTRA_WORKTREE_ROOTS", os.pathsep + os.pathsep)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo evil > /tmp/should-not-write"},
    }
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


# --- force push 合并短选项（-uf / -fu 等含 f 的单横杠簇）也应拦 ---

@pytest.mark.parametrize(
    "command",
    [
        "git push -uf origin main",
        "git push -fu origin main",
        "git push -vf origin HEAD",
        "git push -fq origin HEAD",
    ],
)
def test_force_push_combined_short_flags_blocked(command: str, worktree: Path):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None, f"应拦截合并短选项 force push：{command}"


def test_set_upstream_without_force_allowed(worktree: Path):
    # -u（set-upstream）不含 f，正常推送不应被误拦
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push -u origin feature/x"}}
    assert pretool_guard.evaluate(payload) is None


# --- 把越界写入目标加引号不能绕过“工作目录外禁写” ---
# 修复点：sanitize 会剥掉引号内容（为放行 push option value 里的 /healthcheck），
# 但这也让 `rm "/越界/路径"` 逃过路径扫描。守卫现在对原始命令补扫“内容本身就是
# 绝对路径”的引号词。

@pytest.mark.parametrize(
    "command",
    [
        'rm "/tmp/should-not-write"',
        "cp secret.txt '/etc/cron.d/evil'",
        'tee "/tmp/outside" <<<x',
    ],
)
def test_quoted_absolute_write_target_blocked(command: str, worktree: Path):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason, f"应拦截：{command}"


def test_quoted_inside_worktree_write_allowed(worktree: Path):
    # 引号内是 worktree 内的绝对路径 → 放行（补扫不能误伤合法写）
    inside = worktree / "sub dir" / "f.txt"
    payload = {"tool_name": "Bash", "tool_input": {"command": f'tee "{inside}"'}}
    assert pretool_guard.evaluate(payload) is None


def test_tilde_write_outside_worktree_blocked(worktree: Path):
    # 未加引号的 ~/ 在 bash 里展开到 home（worktree 外）→ 应拦
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo pwned > ~/cc_fleet_guard_probe_outside"},
    }
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


# --- Write/Edit 相对路径的 ../ 穿越应拦 ---

def test_write_relative_parent_traversal_blocked(worktree: Path):
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "../../etc/evil.txt", "content": "x"},
    }
    reason = pretool_guard.evaluate(payload)
    assert reason is not None and "工作目录外" in reason


def test_write_relative_within_worktree_allowed(worktree: Path):
    # 普通相对子路径仍应放行（相对 cwd=worktree 解析后仍在其内）
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "sub/dir/new.py", "content": "x"},
    }
    assert pretool_guard.evaluate(payload) is None
