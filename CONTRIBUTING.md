# 贡献指南（人类贡献者入口）

欢迎给 cc-fleet 提 issue 与 PR。本文件给人类贡献者一个上手入口；AI coding 助手（Claude Code / Codex / Cursor / Aider 等）请直接读 [AGENTS.md](./AGENTS.md)。

## 协作硬约束

> 本项目协作硬约束统一定义于 [AGENTS.md](./AGENTS.md)，所有贡献者请先读一遍。AGENTS.md 是唯一权威源，本文件不复制条文，避免双向漂移。

简要提醒（细节以 AGENTS.md 为准）：

1. **所有改动只在 worktree 或独立 clone 内完成**
2. **任务完成后一次性 push 并自动创建 MR / PR**
3. **以挑剔的态度反问，提升需求质量**
4. **代码改完检查文档与注释同步**
5. **提交 MR / PR 前做必要的测试验证**

## 项目治理

- 这是一个小型开源项目，PR 由维护者人工 review
- 主分支 `main` 受保护，所有改动走 PR / MR
- 大的功能改动建议先开 issue 讨论方向
- 兼容性破坏（CLI 行为变化、配置 schema 变化、数据库 schema 变化）需在 PR 描述里显式标注

## 本地开发环境

```bash
git clone https://github.com/fboostr/cc-fleet.git
cd cc-fleet
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

按 [AGENTS.md](./AGENTS.md) 规则，**改动请在独立 worktree 内做**，不要在主 clone 上直接改：

```bash
git worktree add ../cc-fleet-worktrees/<task-name> -b <branch-name>
cd ../cc-fleet-worktrees/<task-name>
```

## 怎么开 issue

请按模板选择类型：

- **Bug report**：参照 `.github/ISSUE_TEMPLATE/bug_report.yml` —— 至少描述环境、复现步骤、期望 vs 实际、相关日志（`stream.jsonl` 末尾 / `app.log` 片段）
- **Feature request**：参照 `.github/ISSUE_TEMPLATE/feature_request.yml` —— 描述动机、提议方案、备选、关联场景

不确定属于哪类时优先开 Discussion；只有"明确的 bug"或"明确的 feature 提案"才走 issue。

## 怎么提 PR

1. fork 仓库（或如果你有 push 权限，直接在 worktree 里建 feature 分支）
2. 实现 + 测试 + 文档同步（**测试与文档同步是硬约束**，没做要在 PR 描述里明确表态）
3. 按 `.github/pull_request_template.md` 写描述：含「背景 / 用户原始需求 / 改动概要 / 测试与验证 / 文档与注释同步 / 风险与回滚」六小节
4. 标题按 AGENTS.md 第 2 条规范：动宾结构、≤60 字符、带合适的 commit type 前缀（`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`）
5. push 后 GitHub 上检查 CI（如未来加上）

## 测试约定

```bash
.venv/bin/python -m pytest -v
```

几点踩坑提示：

- 项目用 `pytest-asyncio` 的 `auto` mode，async 测试函数无需手动加 `@pytest.mark.asyncio` 装饰
- 涉及 Claude / Git 子进程的测试一律用 `AsyncMock` 替身，绝不调真实 binary
- 你在 worktree 里跑测试时，`.venv` 的 editable install 装的是**主 clone**——如果你修改了 worktree 里的代码而想让测试用上，要么 `PYTHONPATH=src` 跑、要么在 worktree 内再装一次 `pip install -e ".[dev]"`（建议前者，避免污染主 clone 的 .venv）
- 新功能加单测；bug fix 加回归测试

## 文档同步

按 AGENTS.md 第 4 条，行为变化 / 接口签名 / 配置项增删 / 约束变化 / 用户可见流程变化都属于"必须同步文档"。本项目的文档分层：

- `README.md` —— 首页，面向新访客
- `docs/architecture.md` / `state-machine.md` / `reviewer.md` / `remote-mode.md` / `security.md` / `troubleshooting.md` —— 各主题 deep-dive
- `AGENTS.md` —— 协作规范（AI + 人类共用）
- 模块顶 docstring + 关键函数 docstring + 行内注释 —— 代码级文档

改了行为？至少扫一遍 README + 相关 doc + 相关 docstring。

## 协议

提交即同意以 [MIT License](./LICENSE) 许可你的贡献。
