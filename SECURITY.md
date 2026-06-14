# 安全策略

## 当前安全姿态

cc-fleet 让一个 LLM agent（Claude Code）跑在你本机上，可以执行 shell、写文件、操作 git。**当前版本适用于内部信任环境**：

- 单进程主控，只绑 `127.0.0.1`
- 软护栏（`PreToolUse` hook）拦 force push / 工作目录外写 / 敏感路径访问
- 无硬隔离（`sandbox-exec` 等是后续路线）
- 仅设计给内网 / 个人本机使用

**绝不要把 cc-fleet 主控暴露到公网。**

## 已知风险

- **Prompt-injection**：恶意 commit message / 文件内容 / 引用文本能让 claude 在自身工具使用层"自愿"绕过约定
- **Shell 字符串混淆**：经过 base64 / 嵌套 quote 编码的命令可能扫描不到
- **环境变量泄露**：claude 子进程能读到主控传入的所有 env（含 `GITHUB_TOKEN`）
- **网络出口未拦截**：claude 可以 `curl` 任何外网地址
- **远端 dev box 上的写动作**：remote 模式下 `ssh ... 'rm -rf /'` 这类命令本地 hook 拦不到正文里的远端绝对路径（除非匹配 force push / 敏感目录模式）

完整边界、能拦什么、不能拦什么的详解见 [docs/security.md](./docs/security.md)。

## 部署前 checklist

如果你计划把 cc-fleet 暴露给非完全信任用户（如开放到全公司的群聊），请补：

- [ ] **企微 `allowed_chatids` 白名单**：只接信任的群 / 用户 chatid
- [ ] **macOS `sandbox-exec` 硬隔离**：把 claude 子进程关在 sandbox 里（后续路线）
- [ ] **ssh-agent 仅加载需要的 key**：避免 claude 拿到的 SSH 凭据范围超过所需仓库
- [ ] **GitHub token 用 fine-grained**：只给目标仓库 PR 读写权限
- [ ] **HTTP 面板不要改 `bind`**：保持 `127.0.0.1`，远程查看走 SSH 隧道
- [ ] **`stream.jsonl` 按需脱敏 / 轮转**：含 claude 输入输出原文，可能含敏感内容
- [ ] **`workspace_root` 与 `db_path` 文件权限**：必要时手工收紧到 `0600` / `0700`
- [ ] **dev box 上独立加 hook**：remote 模式远端写动作的硬隔离需要 dev box 自己装一层

## 漏洞披露

**不要公开 issue 描述漏洞细节**，避免被利用。

请通过以下任一渠道私下告知：

- 在仓库开 issue 时勾选 "Report a security vulnerability"（GitHub 私密 advisory）
- 给维护者发邮件（请在仓库 README / 项目主页查找当前维护者邮箱）

我们会尽快回复、协调修复时间窗、与你商量 advisory 发布时间。

## 修复优先级

| 严重程度 | 例子 | 目标响应时间 |
|---|---|---|
| 高 | 远程代码执行、凭据泄露、护栏可被远程触发的稳定绕过 | 7 天内回复 + 修复路线 |
| 中 | 单一场景的护栏绕过、需要特定上下文才触发的写越界 | 30 天内回复 |
| 低 | 文档错误导致用户配置成不安全状态、信息泄露但需高权限前提 | 尽力而为 |

## 致谢

我们会在 advisory / release notes 中致谢报告者（除非你要求匿名）。
