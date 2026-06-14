← 回到 [README](../README.md)

# 架构概览

cc-fleet 主控是一个单进程 Python 应用，把 6 个职责装在一起：聊天平台（企微 / 个人微信）接入、消息分类、session 状态机、**可插拔 Agent Runner**（当前实现 Claude Code）子进程驱动、SQLite 持久化、本地 HTTP 只读面板。

## 组件图

```mermaid
flowchart LR
    IM[企业微信] -- WS 长连接 --> Bot[bot/wecom/runner.py]
    Bot -- IncomingMessage --> App[app.py]
    App -- classify --> Disp[core/dispatcher.py]
    Disp -- NEW/CONTINUE/COMMAND --> SM[core/session_manager.py]
    SM -- 后台 task --> Sess[core/session.py]
    Sess -- run --> RUN[core/runners/*<br/>可插拔 Agent Runner]
    RUN -- subprocess --> CC[Claude Code CLI<br/>codex / opencode 待接入]
    Sess -- 状态写入 --> DB[(SQLite)]
    Sess -- stream 落盘 --> JL[stream.jsonl]
    Sess -- git --> GIT[git worktree / push]
    GIT -- MR/PR --> Forge[GitLab / GitHub]
    Sess -- reply --> Bot
    Browser -- HTTP --> Web[web/server.py]
    Web -- 只读 --> DB
    Web -- read --> JL
```

## 调用链

`app.py` 启动时按顺序装配：

1. `Database.connect()` —— 跑 migrations，建表
2. `WecomBotRunner` —— 起 WebSocket，注册 `on_message` 回调
3. `SessionManager(db, config, reply)` —— 持有 semaphore、`_repo_locks`、`_sessions` 字典
4. `WebServer(db, http_cfg, workspace_root).start()` —— 起 aiohttp 监听 127.0.0.1:8787

主消息流（`App._on_message`）：

1. `dispatcher.classify(msg, config, is_open_session)` 把消息归类
2. 按 `DispatchKind` 走分支：
   - `NEW` → `SessionManager.new_session()` 同步建 db 行 + 起后台 task
   - `CONTINUE` → `SessionManager.continue_session()`（awaiting：唤醒已等的 task；resumable_terminal：起新 task 复活）
   - `COMMAND` → `commands.dispatch_command()` 同步算结果
   - `NOISE` → 直接 reply 提示
3. 后台 `_session_loop` acquire semaphore → 反复 `Session.drive()`，遇 awaiting 等 `resume_event`，遇终态退出

## 可插拔 Agent Runner（多 coding agent 支持）

cc-fleet 在架构上把「驱动一个 coding agent」做成可插拔的 runner —— 编排层工具无关，工具耦合集中在一层。目前实现了 **Claude Code**，Codex / opencode 等待接入。

### 设计意图

整条交付链路（session 状态机、协议解析、模式分支 local/remote、worktree·MR）**与具体工具无关**；真正的工具耦合只集中在三处：① 子进程驱动、② 安全护栏、③ 少量配置 / 措辞。因此支持多 agent 是「补一层 runner 抽象 + 逐工具适配」，而非为每个工具复制整条流程。

### 分层（`core/runners/`）

```mermaid
flowchart TB
    SESS[core/session.py<br/>状态机·工具无关] -->|按 RepoConfig.agent 选| FAC[runner_factory.get_runner]
    FAC --> RUNNER[AgentRunner·逐工具实现]
    RUNNER --> ENGINE[runners/engine.py<br/>run_subprocess·工具无关]
    RUNNER --> INTERP[StreamInterpreter<br/>逐工具解析事件]
    RUNNER --> GUARD[GuardrailProvider<br/>逐工具护栏]
    RUNNER -.->|当前实现| CLAUDE[runners/claude.py<br/>ClaudeRunner / ClaudeInterpreter / ClaudeGuardrailProvider]
```

- **`runners/base.py`**：归一接口 —— `AgentRunner`（`run(permission, protocol_text, guardrail, …)`）、`AgentPermission`（READ_ONLY / WRITE，取代 claude 专属的 plan / acceptEdits 字面量）、`AgentRunResult`、`GuardrailProvider` / `GuardrailHandle`。
- **`runners/engine.py`**：**工具无关**子进程引擎 `run_subprocess`（`start_new_session` 进程组回收、chunk 读 stream 避开 readline 64 KiB 上限、超时 SIGTERM→SIGKILL）+ `StreamInterpreter` 协议（「一条事件如何抽文本 / session_id / 终态错误」逐工具实现）。
- **`runners/claude.py`**：Claude 实现 —— `ClaudeRunner` / `ClaudeInterpreter` / `ClaudeGuardrailProvider`。
- **`runner_factory.get_runner(tool, config)`**：按 `RepoConfig.agent` 选 runner。
- **配置层对称**：每个工具一个配置块（`ClaudeConfig`，未来 `CodexConfig` / `OpencodeConfig`，都至少含 `binary`）；工具无关的阶段超时 / 澄清轮次在 `PipelineConfig`。

### tool × mode 正交（加法不是乘法）

工具（claude / codex / …）与模式（local / remote）是两条正交的轴：runner / interpreter / guardrail 随**工具** ×N；模式编排（worktree 落点、SSH、算护栏「允许写的根」）随**模式** ×1，已存在且工具无关。给已支持的工具加新模式、或给已支持的模式加新工具，几乎没有新的 per-(工具 × 模式) 代码 —— 是加法不是乘法。

### 扩展一个新工具

1. 新增 `runners/<tool>.py`：实现 `AgentRunner` + `StreamInterpreter` + `GuardrailProvider`（复用 `engine.run_subprocess`）；
2. `runner_factory.get_runner` 加一个分支；
3. `config/schema.py` 加该工具的配置块（如 `CodexConfig`）；
4. 配置放行 + 实跑验证。

全是**加法**，不回头改已有 claude 代码（`get_runner` 已留好分支位）。

### 现状与 claude 耦合残留

| 工具 | 状态 | 无头 / 流 | 护栏机制 |
|---|---|---|---|
| Claude Code | ✅ 已实现 | `claude -p` / stream-json | PreToolUse hook（settings.json） |
| Codex | 待接入 | `codex exec` / `--json` | OS sandbox（`--sandbox`） |
| opencode | 待接入 | `opencode run` / `--format json` | JS 插件 / permission |

P1 已把「子进程驱动 + 护栏」工具无关化，但有三处 claude 耦合按「逐工具适配」留到接 codex 时、用真实需求驱动解耦（避免无第二个工具时凭空泛化错方向）：

1. **session-id 模型** —— `session.py:_do_new` 预生成 UUID + DB 列 `claude_session_id`。claude 自建 id，codex / opencode 由工具分配，需改为「捕获式」+ 放宽 `util/ids` 的 sid 正则。
2. **prompt / 协议措辞** —— `prompts/*.md` 里 `CLAUDE.md` / 「会被外部 hook 拦截」等。协议**结构**（`SLUG` / `STATUS` / `MR_*` / `REVIEW_VERDICT`）本身工具无关，需中性化**措辞**。
3. **DB / 事件命名** —— `claude_session_id` 列、events 的 `claude.<type>` 前缀。功能上可承载非 claude，命名可选泛化。

> 即：衡量「耦合多深」要看这三处，而非 `ClaudeConfig` 的字段数 —— 后者只是 claude 的工具配置块（对称占位）。

## SQLite schema

三张主表（见 `src/cc_fleet/storage/migrations.py`）：

### sessions

| 列 | 类型 | 说明 |
|---|---|---|
| `slug` | TEXT PK | 内部主键，初始 `req-<时间>-<random>` |
| `display_slug` | TEXT UNIQUE | plan 阶段 claude 返回的可读 slug |
| `repo` | TEXT | 仓库名（与 `config.yaml` 的 `name` 对齐） |
| `state` | TEXT | `SessionState` 枚举字面量 |
| `claude_session_id` | TEXT | Coder 的 claude `--session-id` |
| `reviewer_session_id` | TEXT | 独立 Reviewer 的 claude `--session-id`（懒生成） |
| `worktree_path` | TEXT | local 模式 worktree 绝对路径（主机本地）；remote 模式 = 壳子目录 |
| `branch` | TEXT | `claude/<slug>` |
| `default_branch` | TEXT | 目标分支（一般 `main`） |
| `initial_request` | TEXT | 用户最初输入（已剥 `[review]` 内联指令） |
| `chatid` / `userid` | TEXT | 企微会话 / 用户标识 |
| `clarify_rounds` | INT | plan ↔ awaiting 已发生轮数 |
| `plan_review_rounds` / `code_review_rounds` | INT | Reviewer 已发生轮数 |
| `review_override` | INT NULL | 单需求覆盖：NULL=跟随 repo / 1=强制开 / 0=强制关 |
| `mr_url` | TEXT | MR/PR URL |
| `last_error` / `failed_phase` | TEXT | 失败时记录，决定 follow-up resume 目标 |
| `created_at` / `updated_at` | TEXT | ISO8601 + 本地时区 |

### messages

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INT PK | 自增 |
| `session_slug` | TEXT FK | → `sessions.slug` |
| `direction` | TEXT | `in` / `out` |
| `text` | TEXT | 消息正文 |
| `quote_text` | TEXT | 用户引用的原消息 |
| `ts` | TEXT | 时间戳 |

### events

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INT PK | 自增 |
| `session_slug` | TEXT FK | → `sessions.slug` |
| `kind` | TEXT | `claude.<type>` / `reviewer.<type>` / `state` / `session_started` |
| `payload_json` | TEXT | 事件原文 JSON |
| `ts` | TEXT | 时间戳 |

## HTTP 面板 API

全部 GET、JSON、无写操作，默认 `http://127.0.0.1:8787/`。

| 路径 | 用途 |
|---|---|
| `/api/sessions[?state=open]` | session 列表；`state=open` = 工作中 + awaiting + 可恢复终态 |
| `/api/sessions/{slug}` | 单 session 详情（支持 internal slug 与 display_slug） |
| `/api/sessions/{slug}/events` | 倒序最近 200 条 claude SDK 事件（含 stream-json 原文） |
| `/api/sessions/{slug}/messages` | 全部聊天消息 |
| `/api/sessions/{slug}/plan` | 当前 session 最新 `plan.md` 原文（前端做 markdown 渲染） |
| `/api/sessions/{slug}/plan-review` | Reviewer 对 plan 的审查意见 `plan_review.md` 原文；未启用 / 未产出 → 404 / `no_plan_review` |
| `/api/sessions/{slug}/code-review` | Reviewer 对编码的审查意见 `code_review.md` 原文；未启用 / 未产出 → 404 / `no_code_review` |

### 前端筛选语义

| 组 | 状态 | 默认 |
|---|---|---|
| 工作中 | `new / planning / developing / mr_submitting / plan_reviewing / code_reviewing` | ✅ |
| 等待回复 | `awaiting_user_clarification` | ✅ |
| 已结案（可继续） | `completed / failed / timeout` | ✅ |
| 已关闭 | `cancelled` | ❌ |

前端 Messages 列头部有「Plan」按钮，点开弹出 modal，顶部 tab 在 `Plan / Plan 审查 / Code 审查` 之间切换，分别拉 `/plan`、`/plan-review`、`/code-review` 渲染 markdown；缺文件 tab 显示"暂无（未启用 Reviewer / 该阶段未产出）"（Esc 或点击遮罩关闭）。渲染器是内嵌的迷你实现（约 130 行 JS），覆盖标题/列表/代码块/行内 code/粗体/斜体/链接/引用/分隔线，保持面板"单文件、纯静态、可离线"特性。

## 共用连接

主进程的 `aiosqlite` 单连接被 SessionManager / Database / WebServer 共用（WAL + foreign keys）。WAL 让 web 的只读查询与主控的写并发安全；不开第二个连接是为了避免文件锁竞争。

## 时间戳口径

所有写入 DB 的时间都通过 `util/time.py:now_local_iso()` 产出（含本地时区偏移）。日志格式化器 `util/logging.py:_LocalTZFormatter` 同样输出本地时间 + 时区后缀，跨机器排查时不会丢上下文。
