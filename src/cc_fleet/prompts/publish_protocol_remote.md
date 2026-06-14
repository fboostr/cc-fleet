# cc-fleet 发布阶段协议（remote 模式）

你此前已在远端 worktree 完成代码改动并 `git commit`（**本会话已 resume，上下文延续**）。代码已通过审查（或本 session 未启用代码审查），**现在执行发布**：push 分支并创建 MR/PR。全程使用**中文**。

## 远端环境（主控注入的占位会展开成实际值）

- SSH 别名：`{remote_ssh_alias}`（已配好免密）
- 项目主目录：`{remote_repo_path}`
- worktree 路径：`{remote_worktree_root}/{display_slug}`
- 本 session 分支：`claude/{display_slug}`
- 目标分支：`{default_branch}`

## 发布流程

1. `ssh {remote_ssh_alias}`、`cd {remote_worktree_root}/{display_slug}`；先确认改动已在本分支 commit（`git log origin/{default_branch}..HEAD` 应能看到你的提交）。若发现还有未提交改动，先补 `git commit`。
2. 然后按下面"MR 元数据规范"写好协议块，并 push + 建 MR/PR + 输出 `MR_URL:`：

{forge_workflow}

## MR 元数据规范

回复正文里需要按以下格式输出协议块（让记录与 MR/PR 中能交叉对照），**同时**把同样的标题与描述用于创建 MR/PR（具体方式见上方发布流程：GitLab 经 push option 时 description 需做 `\n` 转义，GitHub 经 `gh` / REST API 时直接传原文）：

```
MR_TITLE: <一行中文标题>

MR_DESCRIPTION_BEGIN
<多行中文 Markdown 描述，按下面"描述规范"的小节模板写>
MR_DESCRIPTION_END
```

### 标题规范

- **是工作内容的概括，不是用户需求的原话**（这是本协议的首要目的）。例：用户说"加一行 readme"，标题应当是 "在 README 顶部新增项目简介行" 而不是 "加一行 readme"
- 动宾结构、单行、≤60 字符
- 不带句号、不带"请帮我 / 我想 / 能否"等祈使语气
- **commit type 前缀按以下规则决定**：
  1. 用只读 bash 探目标仓库是否已有 MR/commit 规范：依次看 `.gitlab/merge_request_templates/*.md`、根目录 `MERGE_REQUEST_TEMPLATE.md`、`CLAUDE.md`、`CONTRIBUTING.md`；再跑 `git log -20 --pretty=%s origin/{default_branch}` 看近期 commit 风格
  2. **若发现项目已有可识别的规范或风格**（含 `feat:/fix:` 前缀、JIRA tag 前缀等），按项目约定写
  3. **若未发现明确规范**，**强制**使用 `feat:/fix:/docs:/refactor:/test:/chore:` 中合适的前缀

### 描述规范

描述用中文 Markdown，按以下小节模板写。**所有小节都必含**——「测试与验证」「文档与注释同步」是项目硬约束，没做也要明确表态（"已验证 X / 未验证 Y" 或 "不涉及文档"），不要省略小节：

```
## 背景
<1-2 句说明本次改动想达成的目标>

## 用户原始需求
> <逐行 quote 用户最初的中文需求，保留上下文>

## 改动概要
- <按模块/文件分点，每条 1 句话，说明改动重点和动机>

## 测试与验证
- <跑了什么测试、用例编号、手动验证步骤、已知盲点；都没做就明确写"已验证 X / 未验证 Y / 已知限制 Z">

## 文档与注释同步
- <"已在本 MR 中同步：xxx" / "不涉及文档" / "待后续单独跟进：xxx" 任一表态>

## 风险与回滚
- <可能的影响范围、回滚要点；无则填"无显著风险">
```

## 本地 PreToolUse 守卫

主控已把本地壳子目录（cwd）、`{remote_repo_path}`、`{remote_worktree_root}` 作为允许写入前缀注入守卫白名单，`ssh {remote_ssh_alias} '…'` 里出现这些远端路径下的写动作应正常放行。真碰到误拦按下方"异常处理"汇报，不要换名规避。

## 异常处理

- ssh 失败、git push 被拒、建 MR 失败等 — **把原始命令和原始报错原样贴在回复里，不要重试、不要换路子**；主控会把回复返还用户处理
- `git push --force` / `--force-with-lease` 仍会被外部 hook 拦截；不要尝试绕过
- 全程使用**中文**：思考、回复、MR 标题与描述
