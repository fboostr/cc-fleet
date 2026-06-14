← 回到 [README](../README.md)

# 独立 Reviewer

cc-fleet 默认只有一个 agent（Coder）既定 plan 又写代码。对关键仓库，可启用一个**独立于 Coder 的 Reviewer agent**，在两个检查点挑剔审查、由 Coder 据意见完善：

1. **plan 审查**：Coder 出 plan（`READY`）后，Reviewer 对照用户原始需求 + 澄清问答审查 plan 是否合理、有无 bug / 遗漏，给出意见；Coder 据此完善 plan
2. **代码审查**：Coder 开发完成后（已 commit、未提 MR），Reviewer 跑 `git diff` 审查实现的正确性、边界、测试与文档同步；Coder 据此修订

无论 Reviewer 返回 `APPROVED` 还是 `NEEDS_REVISION`，主控**都**会把审查正文交回 Coder 让它再完善一轮——因为 `APPROVED` 时正文里也常列出 nit / 可选的小改进，直接放行会丢这些价值。两条路径仅在**给用户的通知文案**与**修订 prompt 的语气**上有差异（"通过后微调" vs "被打回修订"）；最终都进入下一阶段，且 `APPROVED` 路径下次跳过审查直接放行，不会形成「再 APPROVED → 再微调」死循环。

## 设计动机

让 plan / 代码经过一次"挑剔的同行评审"再交付，能有效拦截：

- plan 漏覆盖原始需求 / 跑偏 / 过度设计
- 澄清结论被 Coder 自己忘了
- 边界值、空值、并发、幂等等 corner case 漏考虑
- 测试与文档同步遗漏

为什么不让同一个 Coder 自审？同会话自审会陷入"自己的方案当然对"的确认偏差。独立会话让 Reviewer 从零看，效果显著好。

## 开启方式（仓库级）

在 `config.yaml` 为某个 repo 开启（默认关闭）：

```yaml
repos:
  - name: my-repo
    path: ~/workspace/my-repo
    default_branch: main
    reviewer:
      enabled: true
      max_rounds: 1          # 「审查→修订」轮次上限，默认 1；0 等价于关闭
```

## 单需求级覆盖（`[review]` 内联指令）

`reviewer.enabled` 是**仓库级**默认。如果只想对**某一条需求**临时开/关审查，无需改配置，在需求文本里加内联标记即可（标记会被解析并从需求中剥除，不污染发给 claude 的 prompt）：

```
@my-repo [review] 实现登录接口的限流        # 本次强制开启 Reviewer（即使仓库默认关）
@my-repo [review:off] 修个错别字            # 本次强制关闭 Reviewer（即使仓库默认开）
```

- 标记跟在 `@<repo>` 之后、需求正文任意处均可；`[review]` 等价 `[review:on]`
- 仅对该 session 生效，覆盖仓库默认，不影响其它需求；只在**发起新需求**时解析（进行中 / 已结案 session 不受影响）
- 优先级：单需求标记 > 仓库 `reviewer.enabled`
- 强制开启时即使仓库 `max_rounds` 为 0 也至少审 1 轮；`max_rounds` 本身仍取仓库配置

## 关键约束

- **独立会话**：Reviewer 用独立的 claude 会话（`reviewer_session_id`），与 Coder 会话完全隔离，**plan permission mode 只读**（不改代码，修订一律由 Coder 做）
- **首次审查成功后**才持久化 `reviewer_session_id`，避免早失败留下无法 resume 的幽灵会话 id；后续审查 resume 它以保持上下文连续（plan 审查 → code 审查）
- **状态机**：`planning → plan_reviewing`、`developing → code_reviewing` 两个中间态；`max_rounds` 限制 **`NEEDS_REVISION` 修订循环**轮数，避免来回死循环（`APPROVED` 路径用一次性内存闸门 `_skip_next_*_review` 保证下次直接放行，因此 `APPROVED` 不计入 `max_rounds`）
- **建议而非门禁**：审查 → 修订一轮后即放行，最终以 Coder 修订后的产物为准
- **失败即跳过**：Reviewer 任一环节执行失败 / 超时 / 输出不合协议，直接跳过、当作没有 Reviewer，绝不让 session 失败

## 四种 verdict 处理路径

`run_reviewer()` 跑一次后的处理分支：

| 情况 | 处理 |
|---|---|
| `REVIEW_VERDICT: APPROVED` | 通知用户"审查通过 ✅，Coder 据可选建议最终完善/调整中"；审查正文落 `plan_review.md` / `code_review.md`，作为可选完善建议注入下一轮 Coder prompt（"通过后微调"语气），状态回到 PLANNING / DEVELOPING；**不**累加 `plan_review_rounds` / `code_review_rounds`；置 `_skip_next_*_review` 一次性闸门，使下次走到 PLAN_REVIEWING / CODE_REVIEWING 时直接放行进入下一阶段 |
| `REVIEW_VERDICT: NEEDS_REVISION` | 通知用户"Reviewer 提出修订意见，Coder 据此完善中"；审查正文落 `plan_review.md` / `code_review.md`，作为修订指令注入下一轮 Coder prompt（"被打回修订"语气），状态回到 PLANNING / DEVELOPING；`plan_review_rounds` / `code_review_rounds` +1，达 `max_rounds` 后下次不再审 |
| 失败（exit≠0 / timeout / 异常） | 日志 warning，绕过审查直接进下一阶段。普通失败发"⚠️ Reviewer 审查未完成，已跳过"；若判定为**上下文过长**类失败，通知改为点明根因 + 处置建议（"审查跳过：plan/上下文过长…**未经独立审查**…建议拆分需求或精简 plan"），详见 [troubleshooting.md](./troubleshooting.md) |
| 无法解析 verdict（status=None） | 同上，按失败即跳过处理 |

## 模式范围

- **plan 审查**：local / remote 都生效
- **code 审查**：local / remote **都生效**（remote 经 defer-push：dev 只 commit，审查通过后才单独 push + 建 MR）
- remote 模式下 Reviewer 经 SSH 只读 `git diff` 远端 worktree

代价：remote 发布从 dev 内联拆成单独一次 claude 调用（独立的"发布"阶段），元数据规范从 `dev_protocol_remote.md` 挪到 `publish_protocol_remote.md`。

## 产物

- 审查意见落到 `workspace_root/sessions/<slug>/plan_review.md` / `code_review.md`
- Reviewer claude 的 stream-json 落 `reviewer_stream.jsonl`（与 Coder 的 `stream.jsonl` 分开）
- events 表中 Reviewer 事件 kind 加 `reviewer.` 前缀，与 Coder 的 `claude.` 前缀区分

## 成本

启用后每个 session 约多 2 次 plan 级 Reviewer claude 调用（plan 审查 + 代码审查各一次），以及 1～2 次 Coder 微调调用（即便两轮都 `APPROVED`，Coder 仍会按可选建议各完善一轮）。若触发 `NEEDS_REVISION` 还会按 `max_rounds` 再叠加几次。建议只对关键仓库开启。

## 相关源码

- `src/cc_fleet/core/session.py:_do_plan_reviewing()` / `_do_code_reviewing()` / `_run_reviewer()`
- `src/cc_fleet/core/review.py` —— `REVIEW_VERDICT` 协议解析
- `src/cc_fleet/prompts/plan_review_protocol.md` —— plan 审查协议
- `src/cc_fleet/prompts/code_review_protocol.md` / `code_review_protocol_remote.md` —— 代码审查协议
- `src/cc_fleet/config/schema.py:ReviewerConfig` —— 配置 schema
- `src/cc_fleet/core/dispatcher.py:_extract_review_directive()` —— `[review]` 内联指令解析
