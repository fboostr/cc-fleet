← 回到 [README](../README.md)

# 故障排查

按现象分门别类。每条都先给一个"直接排查命令"，再给最常见的原因。

## 机器人收不到消息

```bash
tail -f $log_dir/app.log
```

排查顺序：

1. **凭据错**：`.env` 里 `WECOM_BOT_ID` / `WECOM_BOT_SECRET` 填错；app.log 会有"企微连接异常"或"认证失败"
2. **chatid 白名单**：`config.yaml` 的 `wecom.allowed_chatids` 不为空但你的群 chatid 不在其中；app.log 有 `忽略不在白名单的 chatid=xxx`
3. **机器人没被加进群**：企微管理后台确认机器人已加到目标群
4. **网络**：cc-fleet 主控所在机器需能访问企微 WebSocket 服务端

## session 卡在 `planning`

```bash
cc-fleet sessions logs <slug>          # 可读运行日志（推荐：去噪 + 工具输入/返回 + 阶段流转 + 失败判决）
cc-fleet sessions logs <slug> --raw    # 或回退看原始 stream.jsonl
tail -200 $workspace_root/sessions/<slug>/stream.jsonl | jq -c .   # 直接看原始底稿
```

常见原因：

- claude 输出了 plan 正文但**没按协议输出 `STATUS: READY` 或 `STATUS: NEED_CLARIFICATION`**：主控解析不到 STATUS → `_fail`。看 stream.jsonl 末尾的 assistant 文本，确认是否真的缺协议字段
- claude 跑超时：超时不再是墙钟一刀切，而是**空闲**三档——无工具在飞超 `pipeline.plan.idle_sec`（默认 300s）、有工具在飞超 `plan.tool_sec`、或总时长超 `plan.hard_cap_sec`。主控发形如"plan 阶段空闲无响应超时 / 工具执行超时 / 总时长超时"的通知，state 走 TIMEOUT；可引用回复重试。想立即掐掉跑飞的 claude 用 `/kill <slug>`（`/cancel` 是软取消，不打断活进程）
- claude 子进程异常退出：看 stream.jsonl 末尾是否有 result 事件含 `is_error: true`；app.log 有 `claude 失败` 详情

## session 卡在 `developing`

同上看 stream.jsonl，多见原因：

- dev 阶段 claude 一直在 retry 同一个失败命令（典型如 ssh 失败、git 命令失败）：空闲/工具档超时收尾后转 FAILED；不想等可 `/kill <slug>` 立即强杀
- dev 结束但没有新提交：主控收尾时按 worktree 是否 dirty 分两种文案——
  - **worktree 干净**（claude 确实没写代码）：报 "dev 阶段结束但 worktree 无新 commit"
  - **worktree 有未提交改动**（写了没 commit）：报 "dev 阶段结束但 worktree 改动未提交：尚有未 commit 改动（成果未丢，见 <worktree>）…"。**成果没丢**，在该 worktree 里手动 `git add` + commit 即可找回。常见根因是 claude 误用了交互版 Claude Code 的心智模型：把构建/测试丢**后台**、用 `ScheduleWakeup` / `Monitor` 想"等完成通知再 commit"，但 dev 是**单发轮次**——子进程一结束，后台任务随进程组被杀、也没有跨轮回调唤醒 session（唯一唤醒是用户回复）。应让 claude 前台同步跑构建、并在本轮结束前 commit（dev 协议已明确要求，见 `src/cc_fleet/prompts/dev_protocol_local.md` 的「单发轮次约束」）

## plan / prompt 过长（上下文超限）

prompt 经子进程 **stdin** 传给 claude（不走命令行 `-p` 位置参数），因此**不受 OS 命令行参数上限**（Linux 单参 128 KiB / macOS argv+env 合计 1 MiB）约束。真正的上限是**模型上下文窗口**（约 200K token，`[1m]` 变体更高）。按当前 plan 体量（数 KB）远不会触及；只有需求 / plan / 代码 diff 异常庞大时才可能撞线。

撞线时是干净的模型层报错（`result` 事件 `is_error: true`，文本形如 `prompt is too long: N tokens > 200000 maximum`），用户可见表现因阶段而异：

| 阶段 | 表现 |
|---|---|
| plan / dev | session 转 FAILED，通知点明「内容过长，超出模型上下文窗口」+ 处置建议（原始模型报错仍附在后面）|
| Reviewer 审查（plan / code） | **审查跳过**（fail-open，session 照常继续），通知改为「Reviewer 审查跳过：plan/上下文过长…**未经独立审查**…建议拆分需求或精简 plan」，而非笼统的「审查未完成」|

处置：**拆分需求、精简 plan 或缩小改动范围后重试**。Reviewer 跳过属 fail-open——本次产物未经独立审查就提了 MR，重要改动建议手动复核或拆小重跑。

## `/resume` 拒绝且给提示

按提示走即可。常见提示与含义：

| 提示 | 含义 |
|---|---|
| 已经在主控内存中（state=...） | 主控没死过，不需要 /resume |
| 正在等你的澄清回复 | 这是 awaiting 状态，引用 plan 反问消息回答即可 |
| 已 completed/failed/timeout | 引用最近一条机器人消息追加内容来唤醒，不需要 /resume |
| 已被取消（cancelled） | 不可恢复，需 @\<repo\> 开新 session |
| 所属仓库不在当前 config | 你删了 / 改了 `repos[]`，找不到原仓库 |
| worktree 已丢失 | local 模式 worktree 目录被你手动删了 |

## MR/PR 提交失败

### GitLab

```bash
cd $workspace_root/sessions/<slug>/worktree
git push -u origin <branch> \
  -o merge_request.create \
  -o merge_request.target=main \
  -o "merge_request.title=test"
```

常见原因：

- **push 权限不足**：远端 GitLab 项目里你这个 user 没有 push 到目标分支的权限
- **远端禁用了 push option**：少见，自建 GitLab 可能整组禁用了
- **description 有真换行字符**：rc=128, "push options must not have new line characters"。主控已经把真换行转义成字面量 `\n`，看 commit 是否绕过了主控自己手提的
- **MR 已存在**：GitLab 对 `merge_request.create` 幂等，已有 MR 时把现 MR 的 URL 写到 stderr，rc=0；主控视为成功

### GitHub

```bash
cd $workspace_root/sessions/<slug>/worktree
git push -u origin <branch>
# 然后用 curl 试一下 REST API（替换 OWNER/REPO/TOKEN/BRANCH）
curl -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/OWNER/REPO/pulls \
  -d '{"title":"test","head":"BRANCH","base":"main","body":"x"}'
```

常见原因：

- **`GITHUB_TOKEN` 未设置**：报 "未找到 GitHub token"。`.env` 中配 `GITHUB_TOKEN` 或 `GH_TOKEN`
- **token 权限不足（fine-grained PAT 三种失败签名）**：push 走 SSH 总能成功，真正卡在 REST API 建 PR。按响应码区分缺哪项权限——
  - `404 Not Found`：仓库没加进 token 的 selected repositories（GitHub 对无权限仓库统一返 404，而非 403）
  - `403 Resource not accessible by personal access token`：缺 `Pull requests: Read and write`（响应头 `x-accepted-github-permissions: pull_requests=write` 会点名）
  - `422 Validation Failed ... not all refs are readable`：缺 `Contents: Read`（建 PR 要读 head/base 两个分支的 ref）

  classic PAT 勾一个 `repo` scope 即可全覆盖。建 PR 失败（403/404）时主控通知里会附一条 `…/compare/<base>...<head>?expand=1` 的 compare URL，照它即可手动建 PR（实现见 `core/mr.py` 的 `github_compare_url`）
- **`remote rejected ... no voting servers succeeded`**：把 GitLab 的 `-o merge_request.*` push option 发给了 GitHub。在 repo 配置里显式写 `platform: github`
- **PR 已存在**：GitHub 返回 422 "A pull request already exists"，主控回查 open PR 并复用 URL，视为成功
- **GitHub Enterprise**：auto 探测会把自有域名当 GitLab，必须显式 `platform: github`

两种平台 session 进 `failed` 后**分支保留**，可手动接管。

## worktree 丢失

如果你手动删了 `workspace_root/sessions/<slug>/worktree`：

- working session 会在下次 drive 时 `_fail`
- 引用回复唤醒会被 `_worktree_intact()` 拒绝并提示 "worktree 已丢失"
- 处理：`@<repo>` 开新 session

## HTTP 面板打不开

```bash
curl -sf http://127.0.0.1:8787/ -o /dev/null && echo "ok" || echo "fail"
ss -ltnp | grep 8787  # macOS 用 lsof -nP -iTCP:8787 -sTCP:LISTEN
```

- `http.enabled` 被改成 `false`
- 端口被别的进程占用 → 改 `http.port`
- 主控没起来 / 已 crash → `ps aux | grep cc-fleet`

## stream.jsonl 太大

claude SDK 事件是全量原文，长 session 可能上百 MB。当前没做轮转，按需手动处理：

```bash
# 看活跃 session 之外的可清理空间
du -sh $workspace_root/sessions/*/stream.jsonl | sort -h

# 手动 gzip / 删除已 completed 的 session 子目录（注意不要删 working 的）
```

## 主控日志位置回顾

| 文件 | 内容 |
|---|---|
| `$log_dir/app.log` | 主控应用日志（连接 / dispatch / drive / 异常） |
| `$workspace_root/sessions/<slug>/session.log` | **人类可读运行日志**：去噪渲染工具输入/返回、阶段流转、失败判决（`cc-fleet sessions logs <slug>` 即读此文件） |
| `$workspace_root/sessions/<slug>/stream.jsonl` | Coder claude 的 stream-json 全量原文（`--raw` / 机读底稿） |
| `$workspace_root/sessions/<slug>/reviewer_stream.jsonl` | Reviewer claude 的 stream-json 全量原文 |
| `$workspace_root/sessions/<slug>/plan.md` | 当前 session 的 plan 正文（剥协议尾） |
| `$workspace_root/sessions/<slug>/plan_review.md` | Reviewer plan 审查意见 |
| `$workspace_root/sessions/<slug>/code_review.md` | Reviewer 代码审查意见 |
| `$db_path` | SQLite 数据库 |
