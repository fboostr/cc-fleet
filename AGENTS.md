# 协作规范

本文档是 cc-fleet 项目的协作规范，**适用于人类贡献者与 AI coding 助手**（Claude Code 通过 `CLAUDE.md` 中的 `@AGENTS.md` 引用自动加载本文件；Codex / Cursor / Aider 等会直接读取本文件）。

## 规则

1. **所有改动只在 worktree 或独立 clone 内完成**。AI 助手与外部贡献者一律不在仓库主 clone 上直接改动、提交、切换分支或新建分支；主目录视为只读。开始任务前，先起一个独立 worktree：

   ```bash
   # 在你的本地 cc-fleet 主 clone 下：
   git worktree add ../cc-fleet-worktrees/{worktree-name} -b {branch-name}
   # 已有分支：
   git worktree add ../cc-fleet-worktrees/{worktree-name} {branch-name}
   ```

   随后 `cd` 到 worktree 路径下改代码、跑测试、提交、推送、创建 MR/PR。合并后用 `git worktree remove` 清理。本约束面向 AI 助手与遵循此规范的贡献者；用户本人手工操作不受此限。

2. **任务完成后一次性 push 并自动创建 MR / PR**。所有 commit 在 worktree 内完成后，**一次性** push：

   - **GitLab origin**：调用 `cc_fleet.core.mr.create_mr_via_push`，或等价的 push option 命令

     ```bash
     git push -u origin <branch> \
       -o merge_request.create \
       -o merge_request.target=<base> \
       -o merge_request.title=<标题> \
       -o merge_request.description=<描述> \
       -o merge_request.remove_source_branch
     ```

     description 中的真换行必须替换为字面量 `\n`，否则 GitLab 服务端会以 "push options must not have new line characters" 拒收；细节见 `src/cc_fleet/core/mr.py:build_push_cmd`。

   - **GitHub origin**：项目已内置 PR 自动创建——先普通 `git push -u origin <branch>`，再走 GitHub REST API 建 PR（需在 `.env` 配 `GITHUB_TOKEN` / `GH_TOKEN`，**不依赖 `gh` CLI**）。平台按 origin URL 自动探测（含 `github.com` → github），也可在 repo 配置里用 `platform: github` 显式指定（GitHub Enterprise 自有域名必须显式配）；细节见 `src/cc_fleet/core/mr.py` 的 `create_pr_via_api` / `create_review_request`。

     > 注意：GitHub **不认** GitLab 的 `-o merge_request.*` push option，会整体拒收 push（`remote rejected ... no voting servers succeeded`）。务必让平台正确分流，不要对 GitHub 仓库套用上面的 GitLab push option 命令。

     > token 权限不足时（push 走 SSH 已成功，但 REST API 建 PR 返 403/404——GitHub 对无权限仓库统一返 404），`create_pr_via_api` 会用 `github_compare_url` 拼出 `…/compare/<base>...<head>?expand=1` 的兜底链接并写进 `MrCreateError` 文案，由 `session._fail` 原样透出到失败通知，便于据此手动建 PR；fine-grained PAT 需显式授予该 repo 的 Pull requests:write + Contents。

   任务中途**不**单独 push；同分支后续追加 commit 时，每次 push 仍带相同 push option（GitLab 对 `merge_request.create` 幂等，已有 MR 的 title/description 会被覆盖更新）。例外：用户明确要求"先别推"时跳过自动 push。

   **MR / PR 标题与描述的质量约束**（落实细节见 `src/cc_fleet/prompts/dev_protocol_local.md` 与 `publish_protocol_remote.md` 中的"MR 元数据规范"段。remote 模式经 defer-push 改造后，dev 阶段只 commit；push + 建 MR 挪到审查后的独立"发布"阶段，元数据规范也随之挪到 `publish_protocol_remote.md`。docker 模式与 local 同流水线——主控本地提 MR，元数据规范见 `dev_protocol_docker.md`，仅编译/运行经 `docker exec` 进容器）：

   - **标题应是工作内容的概括，不是用户需求的原话**（动宾结构、≤60 字符、无句号、无祈使语气）。如目标仓库已有 MR/commit 规范（`.gitlab/merge_request_templates/`、`MERGE_REQUEST_TEMPLATE.md`、`CONTRIBUTING.md` 或近期 commit 风格）按项目约定，否则强制 `feat:/fix:/docs:/refactor:/test:/chore:` 等前缀。
   - **描述按多小节 Markdown 模板写**，必含「背景 / 用户原始需求 / 改动概要 / 测试与验证 / 文档与注释同步 / 风险与回滚」六节。其中"测试与验证""文档与注释同步"是本规范第 4、5 条的硬要求，没做也必须明确表态而非省略小节。
   - 主控基于 claude 在 dev 阶段输出的 `MR_TITLE:` / `MR_DESCRIPTION_BEGIN ... MR_DESCRIPTION_END` 协议块解析；协议缺失时回退到「最近一条 commit subject + git log 拼装」兜底，质量明显下降——优先按协议输出。

3. **以挑剔的态度反问，提升需求质量**。接到需求后不要急着给方案或动手，先用挑剔的视角扫一遍，主动反问需求方：

   - **没想到的问题 / corner case**：边界值、空值、并发、失败重试、幂等、权限边界、历史脏数据等可能没覆盖到的场景。
   - **描述不清 / 有歧义的地方**：术语含义、输入输出契约、"全部 / 一些 / 默认"这类模糊量词、隐含前提，都要逐一对齐到不会再二义为止。
   - **潜在超出预计的影响范围**：上下游依赖、数据迁移、回滚成本、性能 / 容量 / 成本影响、对其它团队或线上功能的连锁影响、安全 / 合规风险。

   **Plan 模式下尤其要落实**：`EnterPlanMode` 后产出的 plan 必须显式列出上述反问与对应假设（用"待确认 / 假设"小节呈现），而不是把模糊点偷偷塞进实现细节里。宁可多一轮反问、晚点 `ExitPlanMode`，也不要带着没对齐的前提进入实施。问题要具体、可回答，避免泛泛而问。

4. **代码改完检查文档与注释同步**。每次代码改动落盘后，主动扫一遍可能受影响的文档与注释——包括但不限于 `README.md`、`AGENTS.md`、`docs/` 目录、CLI 帮助文本、模块 / 函数 docstring、关键代码行内注释。判断标准：行为变化、接口签名变化、配置项增删、约束条件变化、用户可见流程变化都属于需要同步的情况；纯重构、变量重命名、内部实现优化通常不需要改文档。发现要同步的一并在**同一个 MR / PR** 内修，避免代码与文档长期漂移；如果同步改动量大或不确定，至少在 MR / PR 描述中明确表态"文档已同步 / 不涉及 / 待后续单独跟进"任一态度。

5. **提交 MR / PR 前做必要的测试验证**。push 之前，按改动性质执行必要的测试：

   - 涉及 Python 业务逻辑：跑相关 `pytest` 用例，至少覆盖改到的模块；新功能补单测，bug fix 补回归测试。
   - 涉及 CLI / 端到端行为：本地手动跑一遍关键流程，确认无回归。
   - 涉及 GitLab push option / MR 创建本身：用测试仓库验证，避免污染线上 MR。
   - 纯文档 / 注释改动：可豁免测试，但仍要确认 markdown 渲染、链接、代码块语言标识正确。

   验证未通过禁止 push。如果本地环境无法完整复现某项验证，须在 MR / PR 描述里写明"已验证 X / 未验证 Y / 已知限制"，让 reviewer 知道哪些是盲点，而不是在沉默中蒙混过关。
