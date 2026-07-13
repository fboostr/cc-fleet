← 回到 [README](../README.md)

# 安全护栏详解

> ⚠️ 当前版本适用于**内部信任环境**，绝不要暴露公网。下面这份文档讲清楚护栏能拦什么、不能拦什么，以及对外暴露前需要补哪些防御层。

## 当前姿态

cc-fleet 主控让一个 LLM agent（Claude Code / Codex CLI / opencode，按 repo 的 `agent` 配置）跑在你本机上，可以执行 shell、写文件、操作 git。这本身就是一个高权限场景。防御层**按工具不同**（原则：每工具尽力而为 + 显式标注差距）：

**Claude Code —— PreToolUse hook**：

- 不依赖 `--dangerously-skip-permissions` 跳过的 `permissions.deny`（那条路径已绕开）
- 在 claude 调用任何工具前，由本地脚本 `pretool_guard.py` 决定是否放行

这条防御**单层、软**，被 prompt-injection 或 shell 字符串混淆都可能绕过，所以是软护栏，不是硬隔离。本文以下「能拦什么 / 拦不住什么」小节均指这套 hook。

**Codex CLI —— OS sandbox（`--sandbox`）**：只读阶段 `read-only`、写阶段 `workspace-write`（写被内核限制在 worktree 内——macOS Seatbelt / 较新 Linux Landlock，路径隔离**强于** hook 的字符串检查、混淆绕不过）。但机制性缺口与 hook 不同：

- **force push 等网络 / exec 操作不拦**（sandbox 只管文件写）——靠注入 prompt 的纪律条款自律，无机械兜底
- **敏感目录读取不拦**（`read-only` / `workspace-write` 均允许全盘读）
- 非 macOS / 无 Landlock 的 Linux 上退化为**无路径隔离**

以上缺口在启动期由 `validate_runtime` 发 WARN 点明；`agent: codex` 请只在内部信任环境使用。

**opencode —— 纯 prompt 软防护（三工具最弱）**：只读阶段用内置 `plan` agent 限权；写阶段 `--agent build --auto` 下**没有任何机械护栏**——越界写、force-push、敏感路径读写全部只靠注入 prompt 的纪律条款自律（已拍板不上 JS 插件的取舍）。启动期 WARN 点明；`agent: opencode` 仅建议内部信任环境使用。

## 能拦什么

### 1. force push 各形态

```python
git push --force
git push -f
git push --force-with-lease[=xxx]
git push origin +ref:ref      # refspec 加号前缀代表 force
```

不论是直接 Bash 命令、还是包在 `ssh <host> "git push --force"` 里，都会命中拦截。

### 2. 敏感路径访问

| 路径 | 形态 |
|---|---|
| `~/.ssh` | 含读 / 写 / `ls` / `cat` 等任何子操作 |
| `~/.aws` | 同上 |
| `~/.config/gh` | 同上 |
| `/etc/passwd` / `/etc/shadow` / `/etc/sudoers` | 系统敏感文件 |
| `/.ssh` | 兜底（任意根级 .ssh 子目录） |

### 3. Write / Edit / NotebookEdit 越界

claude 的 Write / Edit 工具传入的 `file_path` 必须在某个白名单根目录内，否则拒绝。白名单由 `CC_FLEET_WORKTREE` 与 `CC_FLEET_EXTRA_WORKTREE_ROOTS` 注入。

### 4. Bash 启发式 + 绝对路径越界写

识别"写动作 token"（`rm` / `mv` / `cp` / `tee` / `install` / `chmod` / `chown` / `truncate` / `ln` / `dd` / `>>` / `>` / `<<<`）出现且命令中含**绝对路径**时，校验该路径必须在白名单根内，否则拒绝。

扫描前会做三步 sanitize：

1. 剥 heredoc body（避免 yaml 注释里的 `/xxx` 字面量误触发）
2. 剥 single/double quoted 字面量（避免 push option value 里的 slash command 误触发）
3. 剥 fd-dup（`2>&1` 不是文件写入）

## 不能拦什么

- **Prompt-injection**：恶意 commit message / 文件内容能让 claude 在自身的工具使用层"自愿"绕过约定（比如把要写入的路径拼成相对路径回避绝对路径检测）
- **Shell 字符串混淆**：`/bin/sh -c "$(echo c... | base64 -d)"` 这类经过编码的命令体扫描不到
- **写入是 stdout 重定向但路径在白名单内**：白名单允许写自己 worktree 是设计意图，但 claude 仍可以在 worktree 内写任意文件
- **网络出口**：没有拦截网络访问；claude 可以 `curl` 任何外网地址
- **环境变量泄露**：claude 能读到主控传入的所有 env（含 `GITHUB_TOKEN`）
- **远端 dev box 上的写动作**：remote 模式下 `ssh ... 'rm -rf /'` 这类命令本地 hook 拦不到正文里的远端绝对路径，除非该路径明显匹配 force push / 敏感目录模式

## 环境变量注入语义

主控启动 claude 子进程时传入：

| 变量 | 用途 |
|---|---|
| `CC_FLEET_WORKTREE` | 主 worktree（local 模式 = 本地真实 worktree；remote 模式 = 本地壳子目录） |
| `CC_FLEET_EXTRA_WORKTREE_ROOTS` | 额外白名单路径前缀清单，`os.pathsep` 分隔。remote 模式下主控注入 `remote_repo_path` 与 `remote_worktree_root`，让 ssh 包裹里的远端绝对路径不被误判越界 |
| `CC_FLEET_HOOK_PYTHON` | 可选；指定 PreToolUse hook 跑用的 Python 解释器（默认用 `sys.executable`） |

## 对外暴露前 checklist

如果你计划把 cc-fleet 主控暴露给非完全信任用户（如开放到全公司的群聊），请补：

- [ ] **企微 `allowed_chatids` 白名单**：只接信任的群 / 用户 chatid，避免任意人能丢需求进来
- [ ] **macOS `sandbox-exec` 硬隔离**：把 claude 子进程关在 sandbox 里，限定能写的根目录与能访问的网络（后续路线）
- [ ] **ssh-agent 仅加载需要的 key**：避免 claude 拿到的 SSH 凭据范围超过所需仓库
- [ ] **GitHub token 用 fine-grained**：只给目标仓库 PR 读写权限，不要用 classic PAT `repo` 全权限
- [ ] **HTTP 面板不要改 `bind`**：保持 `127.0.0.1`，需要远程查看请走 SSH 隧道
- [ ] **应用日志按需脱敏**：`stream.jsonl` 全量原文包含 claude 的输入输出，可能含敏感内容；按团队合规要求轮转 / 加密 / 不可读
- [ ] **`workspace_root` 与 `db_path` 文件权限**：默认是 `0644` / `0755`，必要时手工收紧到 `0600` / `0700`
- [ ] **dev box 上独立加 hook**：remote 模式下远端写动作的硬隔离需要在 dev box 自己装一层（暂未在本版本中提供）

## 漏洞披露

请见 [SECURITY.md](../SECURITY.md)。

## 相关源码

- `src/cc_fleet/security/permissions.py` —— 渲染并落 `settings.json` 注入 hook
- `src/cc_fleet/security/hooks/pretool_guard.py` —— 当前唯一硬限制点（PreToolUse）
- `tests/test_pretool_guard.py` —— 全面单测覆盖：force push 各形态、敏感路径、heredoc / quote / fd-dup sanitize、白名单 / 越界
