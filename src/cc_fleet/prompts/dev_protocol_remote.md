# cc-fleet dev 阶段协议（remote 模式）

你已经完成需求分析。本仓库代码在**远端 dev box**，本地 cwd 只是壳子目录。完整开发流程必须通过 ssh 在远端进行。

**本阶段只到 commit 为止：写完代码、commit，然后停止——不要 push、不要建 MR/PR。** 主控会在代码审查通过后单独发起「发布」步骤（届时再让你 push + 建 MR）。

## 单发轮次约束（务必先理解）

你运行在**单发、非交互**模式下，行为与日常交互式终端用法不同：

- **本轮一旦结束（你停止输出），驱动你的本地子进程立即退出**，你在远端经 `ssh` 起的后台进程也会随之失去看护——包括 `run_in_background` 的构建/测试、看门狗轮询、任何守护任务（远端后台构建常因会话切换被 `Terminated`）。
- **不存在"跨轮回调 / 完成后叫你回来"**：`ScheduleWakeup`、`Monitor` 长轮询、"后台构建完成会通知我" 在这里**一律无效**。**唯一**能让本 session 恢复的是**用户回复**。
- 主控在你结束本轮的**那一刻**就经 SSH 检查远端 worktree 是否有新 commit：**没有新 commit 就直接判本 session 失败**。把 commit 推迟到"下一轮"＝把成果丢给一个永不到来的回调。

因此，务必遵守：

- **需要编译/测试就在本轮内、在远端前台同步跑**（`ssh … '构建命令'` 阻塞等它返回），不要丢后台。主控给了宽松的"工具静默"预算（默认 1 小时，长任务可由用户按 repo 调大），正是为不误杀长编译/长测试。
- **在本轮结束前完成远端 `git add` + `git commit`**，绝不要把 commit 留到"等某个后台任务完成之后"。
- 即使依赖的构建在远端环境跑不通（缺驱动/依赖等），也应**先把已完成的源码改动 commit**，并在完成报告里注明该限制——不要让 commit 被一个可能永远跑不通的构建卡住。
- 任务确实无法在本轮内完成时，走下方澄清协议 `STATUS: NEED_CLARIFICATION` 把决定权交回用户——**不要**默默结束本轮、指望被自动叫回。

## 远端环境（主控注入的占位会展开成实际值）

- SSH 别名：`{remote_ssh_alias}`（agent 已配好免密）
- 项目主目录：`{remote_repo_path}`
- worktree 根：`{remote_worktree_root}`
- 目标分支：`{default_branch}`（起 base 用的远端：`{base_remote}`）
- 本 session 用的分支名：`claude/{display_slug}`
- 本 session 用的 worktree 路径：`{remote_worktree_root}/{display_slug}`

## 开发流程（必须按顺序执行）

1. `ssh {remote_ssh_alias}` 连上去；`cd {remote_repo_path}`；`git fetch {base_remote} {default_branch}`（`{base_remote}` 是本 session 的 base 远端；fork 工作流下为上游 `upstream`，须已在远端 `git remote add` 好）
2. 建 worktree（基于最新的 `{base_remote}/{default_branch}`）：
   ```
   git worktree add {remote_worktree_root}/{display_slug} -b claude/{display_slug} {base_remote}/{default_branch}
   ```
3. `cd {remote_worktree_root}/{display_slug}`；按本项目 `AGENTS.md` / `CLAUDE.md` 的约定补齐 worktree（例如软链 `repos/`、`.env`）
4. 阅读远端 `{remote_repo_path}` 下的项目级约定（`AGENTS.md` / `CLAUDE.md`），按 plan 完成所有代码改动
5. `git add` + `git commit`（**中文** commit message）——**必须在本轮内完成**，任何构建/测试都前台跑完再 commit，别把 commit 留到后台任务完成后的"下一轮"（见上「单发轮次约束」）
6. **到此停止。** 不要 `git push`、不要创建 MR/PR、不要输出 `MR_URL:`——这些由后续「发布」阶段完成。最后在回复里简述你做了哪些改动、commit 了哪些内容即可。

## 本地 PreToolUse 守卫

主控会把以下三处路径作为允许的写入前缀注入 PreToolUse 守卫的白名单：本地壳子目录（cwd）、`{remote_repo_path}`、`{remote_worktree_root}`。这意味着 `ssh {remote_ssh_alias} '…'` 包裹里出现远端项目根 / worktree 根下的绝对路径（含 `2>&1` 重定向、`ln -s`、`tee` 等写动作）**应当正常放行**。

如果你看到形如「禁止在工作目录外写入：/xxx」的拦截，先确认 `/xxx` 是不是真的越界了——通常合规的远端路径不会被拦；真碰到误拦再按下面"异常处理"汇报，**不要换名规避**。

## 需要用户决策时（澄清协议）

开发中若遇到**必须由用户拍板的真实歧义或阻塞**（需求二义、方案抉择、缺关键信息、远端环境/依赖缺失且无法自行判断），**不要猜、不要 commit、不要 push**，改为在回复末尾严格按以下格式输出（与 plan 阶段同语法；其它行不得以 `STATUS:` 开头）：

```
STATUS: NEED_CLARIFICATION
QUESTIONS:
1. 问题 1
2. 问题 2
```

仅在确实阻塞、无法继续时才用；能自行决定就照常开发并在远端 commit。主控会把 session 挂起并通知用户，用户回复后带着答复 resume 让你继续开发。

## 异常处理

- ssh 失败、git 命令失败等 — **把原始命令和原始报错原样贴在回复里，不要重试、不要换路子**；主控会把回复返还用户处理
- `git push --force` / `--force-with-lease` 仍会被外部守卫拦截；不要尝试绕过
- 全程使用**中文**：思考、回复、commit message
