"""PreToolUse 钩子：当前版本的唯一硬限制点。

`--dangerously-skip-permissions` 会跳过 settings 里的 permissions.deny，
但**不会跳过 hooks**。所以需求 7 的"工作目录外禁写 + 禁 force push"必须落在这里。

约定：主控启动 claude 子进程时，通过环境变量传入允许写入的路径前缀清单。
- `CC_FLEET_WORKTREE`：主 worktree 绝对路径（local 模式 = 本地真实 worktree；
  remote 模式 = 本地壳子目录，仅做 cwd 用）。
- `CC_FLEET_EXTRA_WORKTREE_ROOTS`（可选）：额外允许的路径前缀清单，以
  `os.pathsep` 分隔。remote 模式下主控会把远端项目根（`remote_repo_path`）和
  远端 worktree 根（`remote_worktree_root`）注入这里，避免 claude 通过
  `ssh <host> '…'` 操作远端绝对路径时被本地 hook 当作"工作目录外的写"误拦。

调用约定（claude PreToolUse hook 协议）：
- stdin 读 JSON：{"tool_name": "...", "tool_input": {...}, ...}
- 通过：exit 0，不输出
- 拒绝：stdout 输出 {"decision":"block","reason":"..."}，exit 2

设计上保守优先：宁可误拦也不放过。所有拒绝原因都用中文，便于在企微对话回显。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# ---- 规则定义 ----

# 1. 各种 force push 形态（git 与 push 之间允许 -c xxx=yyy 等任意全局选项）
#    - git push --force
#    - git push -f / -uf / -fu（合并短选项里含 f 即代表 force——push 的短选项里只有
#      -f 表示 force，故任意含 f 的单横杠簇都按 force 拦）
#    - git push --force-with-lease[=xxx]
#    - git push origin +ref:ref（refspec 加号前缀代表 force）
FORCE_PUSH_PATTERNS = [
    re.compile(r"\bgit\b[^\n]*?\bpush\b[^\n]*?--force(?:-with-lease)?(?:\s|=|$)"),
    re.compile(r"\bgit\b[^\n]*?\bpush\b[^\n]*?\s-[a-zA-Z]*f[a-zA-Z]*(?:\s|=|$)"),
    re.compile(r"\bgit\b[^\n]*?\bpush\b[^\n]*?\s\+[^\s:]+:[^\s]+"),
]

# 2. 敏感路径读写（即便在 worktree 内执行也禁止）
SENSITIVE_PATH_PATTERNS = [
    re.compile(r"~/\.ssh(?:/|\s|$)"),
    re.compile(r"~/\.aws(?:/|\s|$)"),
    re.compile(r"~/\.config/gh(?:/|\s|$)"),
    re.compile(r"/etc/(passwd|shadow|sudoers)\b"),
    re.compile(r"/\.ssh(?:/|\s|$)"),
]

# 3. Bash 启发式：识别"写动作 + 绝对路径不在白名单内"
#    注意：扫描对象是经过 sanitize 流程后的字符串——依次剥 heredoc body、
#    single/double quoted 字面量、`\d>&\d?` 这种 fd-dup（参见 check_bash），
#    避免把 yaml 注释 / push option value 里的 `/xxx` 字面量误当成写入目标。
WRITE_TOKENS = re.compile(
    r"\b(rm|mv|cp|tee|install|chmod|chown|truncate|ln|dd)\b|>>?|<<<"
)

# fd-dup（如 `2>&1` / `1>&2` / `2>&`）只是描述符复制，不是文件写入；放进 sanitize
# 流程里剥掉，避免它们触发 WRITE_TOKENS 后误启动路径扫描。
_FD_DUP = re.compile(r"\d>&\d?")

# 安全伪设备 / 标准流：向其重定向写入是无副作用的丢弃或写标准流，不算越界写入。
# 仅豁免这些具名伪设备与 /dev/fd/<n>；/dev/sda 等真实块设备不在内，dd of=/dev/sda 仍会被拦。
# （典型误拦场景：`grep ... 2>/dev/null` 里 `2>` 的 `>` 触发 WRITE_TOKENS，再把 `/dev/null`
#  当成越界写入目标。）
SAFE_DEVICE_PATHS = frozenset(
    {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero", "/dev/full"}
)


def _is_safe_device(token: str) -> bool:
    """token 是否为安全伪设备 / 标准流（写入无副作用），如 /dev/null、/dev/fd/2。"""
    return token in SAFE_DEVICE_PATHS or token.startswith("/dev/fd/")


# 候选写入目标路径 token：绝对路径 `/...`，或 shell 会展开成 home 的 `~/...`
#（未加引号的 `~/` 在 bash 里展开到 home，同样可能落在 worktree 外）。在剥过
# heredoc / 引号 / fd-dup 的 sanitized 串上扫描。
_PATH_TOKEN = re.compile(r"(?<![\w/])~?/[\w./~-]+")

# 「内容本身就是绝对路径」的引号词（引号紧跟 `/`）。sanitize 会把引号整段抹掉以放行
# push option value 里的 `/healthcheck` 等字面量，但这也让 `rm "/越界/路径"` 这类
# **把写入目标加了引号**的越界写逃过路径扫描。故对原始命令补扫这类引号词。
# push option value（内容以 `merge_request.` 等词起头、引号后不是 `/`）不命中，放行不受影响。
_QUOTED_ABS_PATH = re.compile(r"""(['"])(/[^'"]*)\1""")

# 真正的写命令（区别于 `>`/`>>` 纯重定向运算符）。补扫引号绝对路径时以「确有写命令」为
# 前提，避免把 `grep "/x" 2>/dev/null` 这类读命令里的引号读参数误当成写目标。
_WRITE_COMMANDS = re.compile(r"\b(rm|mv|cp|tee|install|chmod|chown|truncate|ln|dd)\b")

# 重定向运算符后紧跟的引号绝对路径（写入目标），如 `> "/越界"` / `>> '/越界'`。
_REDIRECT_TO_QUOTED_ABS = re.compile(r">>?\s*(['\"])(/[^'\"]*)\1")


def _flag_outside_write(token: str, allowed_roots: list[Path]) -> str | None:
    """单个候选写入目标：安全伪设备放行；绝对（或展开后）越出白名单则返回拦截原因，否则 None。"""
    token = token.strip()
    if not token or _is_safe_device(token):
        return None
    abs_path = Path(token).expanduser()
    if abs_path.is_absolute() and not _is_within(abs_path, allowed_roots):
        return f"禁止在工作目录外写入：{token}"
    return None

# 识别 heredoc 起始符：<<TAG / <<-TAG / <<'TAG' / <<"TAG" / <<\TAG。
# 用 (?!<) 排除 here-string (<<<)，后者是单行字面量而非多行 body，不需要剥离。
_HEREDOC_START = re.compile(
    r"<<(?!<)(-?)\s*"
    r"(?:'([^'\n]+)'"
    r"|\"([^\"\n]+)\""
    r"|\\?([A-Za-z_]\w*))"
)


def _strip_heredoc_bodies(command: str) -> str:
    """把命令中所有 heredoc body 替换为单个空格，保留起始/结束 tag 行。

    用于在路径扫描前移除 here-doc 正文中的字面量噪声（如 yaml 注释里的 `/xxx`），
    避免被 check_bash 的绝对路径正则误识为越界写入目标。

    保守策略：遇到无匹配结束行的异常 here-doc，剥到字符串末尾（宁可少扫一些 token
    也不要回头去匹配正文）。
    """
    out: list[str] = []
    pos = 0
    while True:
        m = _HEREDOC_START.search(command, pos)
        if m is None:
            out.append(command[pos:])
            break
        # heredoc body 从起始 tag 行末的 \n 之后才开始。起始行本身（含 `<<TAG`
        # 之后同行的 redirect 目标，比如 `<<EOF > /tmp/x`）必须保留下来继续扫，
        # 否则会漏掉同行越界 redirect 的拦截。
        nl = command.find("\n", m.end())
        if nl == -1:
            # 起始 tag 后没换行：异常 / 命令被截断；保守起见整体保留，不剥
            out.append(command[pos:])
            break
        out.append(command[pos:nl])
        dash = m.group(1) == "-"
        tag = m.group(2) or m.group(3) or m.group(4) or ""
        if dash:
            end_re = re.compile(rf"\n\t*{re.escape(tag)}[ \t]*(?:\n|$)")
        else:
            end_re = re.compile(rf"\n{re.escape(tag)}[ \t]*(?:\n|$)")
        em = end_re.search(command, nl)
        if em is None:
            out.append(" ")
            break
        out.append(" ")
        pos = em.start() + 1
    return "".join(out)


def _strip_shell_quotes(command: str) -> str:
    """剥离 single/double-quoted 字符串内容，整个 quoted 区段（含 quote 字符）
    替换为单空格。

    - single-quote `'…'`：bash 内不允许转义，找到下一个 `'` 即闭合
    - double-quote `"…"`：处理 `\\"` / `\\\\` 转义
    - 不闭合的 quote：保守起见从 quote 起点剥到字符串末尾

    在路径扫描前调用，避免把 push option value、字符串字面量等里出现的 `/xxx`
    （如企微 slash command `/healthcheck`）误识为越界写入目标。FORCE_PUSH 与
    SENSITIVE_PATH 仍跑在原命令上做兜底——参见 check_bash。
    """
    out: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            # quote 外的 backslash 转义：保留两字符
            out.append(command[i : i + 2])
            i += 2
            continue
        if c == "'":
            j = command.find("'", i + 1)
            if j == -1:
                out.append(" ")
                break
            out.append(" ")
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if command[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if command[j] == '"':
                    break
                j += 1
            if j >= n:
                out.append(" ")
                break
            out.append(" ")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_fd_dup(command: str) -> str:
    """把 fd-dup（如 `2>&1`）替换为空格，避免它误触 WRITE_TOKENS。"""
    return _FD_DUP.sub(" ", command)


def _block(reason: str) -> None:
    """输出阻止 JSON 并以 exit 2 结束。"""
    payload = {"decision": "block", "reason": reason}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(2)


def _approve() -> None:
    sys.exit(0)


def _resolve_safely(value: str) -> Path | None:
    """把环境变量里的路径字符串归一化为 Path；不存在的远端路径也接受。"""
    if not value:
        return None
    try:
        return Path(value).expanduser().resolve()
    except OSError:
        return None


def _allowed_roots() -> list[Path]:
    """汇总所有允许写入的路径前缀，保序去重。

    优先级：CC_FLEET_WORKTREE → CC_FLEET_EXTRA_WORKTREE_ROOTS。
    extra 用 `os.pathsep` 分隔；remote 模式下主控会把 remote_repo_path 与
    remote_worktree_root 写进来当远端白名单前缀。
    """
    roots: list[Path] = []
    primary = _resolve_safely(os.environ.get("CC_FLEET_WORKTREE", ""))
    if primary is not None:
        roots.append(primary)

    extra_raw = os.environ.get("CC_FLEET_EXTRA_WORKTREE_ROOTS", "")
    for piece in extra_raw.split(os.pathsep):
        candidate = _resolve_safely(piece.strip())
        if candidate is not None:
            roots.append(candidate)

    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        key = str(r)
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def _is_within(path: Path, roots: list[Path]) -> bool:
    """path 是否落在 roots 中任一前缀下。roots 为空时永远返回 False。"""
    if not roots:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except (ValueError, OSError):
            continue
    return False


def check_bash(command: str, allowed_roots: list[Path]) -> str | None:
    """返回阻断原因；None 表示通过。"""
    for pat in FORCE_PUSH_PATTERNS:
        if pat.search(command):
            return "禁止 force push（包括 --force / -f / --force-with-lease / refspec 加号前缀）。"

    for pat in SENSITIVE_PATH_PATTERNS:
        if pat.search(command):
            return "禁止访问敏感目录（~/.ssh、~/.aws、/etc 等）。"

    if not allowed_roots:
        # 没注入任何白名单：只做与路径无关的检测（force push / 敏感目录），其余放行
        return None

    # 写动作 + 绝对路径不在任一白名单内 → 拦
    # sanitize 三连：剥 heredoc body → 剥 single/double-quoted 字面量 → 剥 fd-dup。
    # 这样 yaml 注释里的 `/healthcheck`、push option value 里的 slash command、
    # `2>&1` 这种 fd-dup 都不会误触发"绝对路径越界写入"判定。
    sanitized = _strip_heredoc_bodies(command)
    sanitized = _strip_shell_quotes(sanitized)
    sanitized = _strip_fd_dup(sanitized)
    if WRITE_TOKENS.search(sanitized):
        # (a) sanitized 里裸露的绝对路径 / 展开后越界的 ~/ 路径。
        #     /dev/null 等安全伪设备是无副作用的丢弃/标准流目标，重定向到它们（如
        #     `2>/dev/null`）不算越界写入（helper 内豁免）。`/dev/null/../etc/passwd`
        #     这类穿越因不等于白名单项、也不以 /dev/fd/ 开头而仍会被拦。
        for token in _PATH_TOKEN.findall(sanitized):
            reason = _flag_outside_write(token, allowed_roots)
            if reason:
                return reason
        # (b) 引号内被当作写入目标的绝对路径（sanitize 抹掉引号后 (a) 扫不到）。只在
        #     「确有写命令」（如 `rm "/x"` / `cp a "/x"`）时补扫全部引号绝对路径；否则
        #     仅补扫「重定向直接指向引号绝对路径」（如 `> "/x"`）。这样 `grep "/x"
        #     2>/dev/null` 这类读命令里的引号读参数不会被误判成越界写。
        if _WRITE_COMMANDS.search(sanitized):
            for m in _QUOTED_ABS_PATH.finditer(command):
                reason = _flag_outside_write(m.group(2), allowed_roots)
                if reason:
                    return reason
        for m in _REDIRECT_TO_QUOTED_ABS.finditer(command):
            reason = _flag_outside_write(m.group(2), allowed_roots)
            if reason:
                return reason
    return None


def check_file_write(file_path: str, allowed_roots: list[Path]) -> str | None:
    """Write / Edit / NotebookEdit 的 file_path 必须在某个白名单根内。"""
    if not file_path:
        return "缺少 file_path"
    p = Path(file_path).expanduser()
    if not p.is_absolute():
        # 相对路径按 cwd（= 主 worktree，白名单首项）解析后再校验：claude 的 cwd 就是
        # worktree，普通相对路径落在其内可放行，但 `../../x` 这类相对穿越会逃出 worktree，
        # 不能一律放行。无白名单时无从确定 cwd，维持旧的宽松放行。
        if not allowed_roots:
            return None
        if not _is_within(allowed_roots[0] / p, allowed_roots):
            return f"禁止在工作目录外写入：{file_path}"
        return None
    if not allowed_roots:
        # 未注入任何白名单，但写的是绝对路径 — 保守起见拒绝
        return "未传 CC_FLEET_WORKTREE，无法判定路径是否合法，拒绝。"
    if not _is_within(p, allowed_roots):
        return f"禁止在工作目录外写入：{file_path}"
    return None


def evaluate(payload: dict) -> str | None:
    """主入口纯函数：返回阻断原因或 None。便于单测。"""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    allowed_roots = _allowed_roots()

    if tool_name == "Bash":
        return check_bash(tool_input.get("command", "") or "", allowed_roots)
    if tool_name in {"Write", "Edit", "NotebookEdit"}:
        return check_file_write(tool_input.get("file_path", "") or "", allowed_roots)
    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # 协议异常时保守放行（避免 hook 异常导致 claude 假死）
        _approve()
        return

    reason = evaluate(payload)
    if reason is not None:
        _block(reason)
    _approve()


if __name__ == "__main__":
    main()
