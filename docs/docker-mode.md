← 回到 [README](../README.md)

# 容器内构建仓库（mode=docker）

部分项目的代码在本地，但**编译 / 测试 / 运行**依赖一个 docker 容器里的工具链（特定编译器、系统库、运行时）。docker 模式面向这种场景：代码文件在主机，并 bind-mount 进一个**运行中的**容器；claude 在主机本地查看 / 修改代码、`git` 提交，**仅**把编译 / 运行类命令经 `docker exec` 丢进容器执行。

> docker 模式 = local 模式 + 「编译 / 运行经 docker exec」。worktree、commit、push、提 MR/PR 全在主机本地，与 local 完全一致；唯一差异是 dev 阶段的命令包裹。

## 快速上手：用一个现成镜像 3 步起容器

> 如果你已经有一个装好工具链的 docker 镜像（下面记作 `A`），照这 3 步就能让 cc-fleet 用它编译 / 运行你的仓库，不需要懂 docker 的高级用法。

**前提**：镜像 `A` 里装了**这个项目编译 / 测试所需的工具链**（编译器、构建工具、运行时）和 `bash`。**不需要**装 git 或 claude——读代码、改代码、`git commit` 全在你主机本地完成，只有编译 / 测试 / 运行才进容器。

**第 1 步：想清楚挂载哪个目录。** cc-fleet 每个任务会在 `<path>-worktrees/<slug>`（你仓库 `path` 的兄弟目录）下新建一个 worktree。容器必须能看到这些 worktree，所以 bind-mount 要**同时覆盖 `path` 和它的 `-worktrees` 兄弟目录**——最省事的是挂它俩的公共父目录。例如仓库在 `~/workspace/my-project`，就把 `~/workspace` 整个挂进容器的 `/workspace`。

**第 2 步：在 `config.yaml` 里写好这个仓库。**

```yaml
repos:
  - name: my-project
    path: ~/workspace/my-project               # 本地 git 仓库（必须含 .git）
    default_branch: main
    keywords: [my-project]
    mode: docker
    docker_container: my-project-dev           # 容器名，cc-fleet 用它 inspect / exec
    docker_host_root: ~/workspace              # 主机挂载根
    docker_container_root: /workspace          # 它在容器内的挂载点
    # 让 cc-fleet 每个任务前自动起容器、跑完自动销毁（用镜像 A）：
    docker_start_command: "docker run -d --name my-project-dev -v ~/workspace:/workspace A tail -f /dev/null"
    docker_stop_command:  "docker rm -f my-project-dev"
```

`docker run` 里几个参数的作用（不熟悉 docker 的话重点看这里）：

| 参数 | 作用 |
|---|---|
| `-d` | 后台运行容器，不占住当前终端 |
| `--name my-project-dev` | 给容器起名，**必须和 `docker_container` 一模一样**——cc-fleet 全靠这个名字 `docker inspect` / `docker exec` 找到它 |
| `-v ~/workspace:/workspace` | bind-mount：把主机 `~/workspace` 映射进容器 `/workspace`，与上面的 `docker_host_root` / `docker_container_root` 对应 |
| `A` | 你的镜像名 |
| `tail -f /dev/null` | **保活命令**：容器的入口进程一退出容器就停，所以必须挂一个永不结束的前台进程把它撑住（`sleep infinity` 同效） |

**第 3 步：照常给这个仓库派任务。** 触发任务后，cc-fleet 会在开发开始前自动用上面的 `docker run` 起好容器，claude 在容器里编译 / 测试，任务结束（无论成败）再自动 `docker rm -f` 销毁。你**不用**手工敲任何 docker 命令。

> 不想让 cc-fleet 自动起停、打算自己常驻一个容器？那就**删掉** `docker_start_command` / `docker_stop_command` 两行，改用下一节的「方式一」。

## 两种容器管理方式

容器由谁起、由谁销毁，取决于你**配不配** `docker_start_command` / `docker_stop_command` 这对字段（二者必须同配或同不配，配置校验会强制这一点）。

### 方式一：手工起停（不配这对字段）

容器由**你自己**预先起好并保持运行（手动 `docker run`、`docker compose up -d` 或任何方式都行），cc-fleet 全程**只 `docker exec` 进去**，绝不替你 `run / start / stop / rm`，任务结束也不动它。适合**一个长期常驻、反复复用**的开发容器。

### 方式二：自动起停（配齐这对字段）

cc-fleet 接管容器生命周期：

- **开发前**：先 `docker inspect` 看容器是否已在运行——已运行就直接复用（幂等，防上次异常退出留下的残壳）；否则执行你的 `docker_start_command`，再轮询最多 60 秒等它进入 running，**起不来或超时就让本次任务失败**，不会带病开工。
- **开发后**：无论成功 / 失败 / 超时，都执行 `docker_stop_command` 销毁容器；销毁失败只记日志，不影响任务结论。

适合**每个任务都要一个干净容器、跑完即焚**的场景。`docker_start_command` 是任意 shell 命令，除了 `docker run`，也可以是 `docker compose up -d`、`docker start <已有容器>` 或一个自定义脚本。

> **常见坑**：方式二下若容器「**存在但已停止**」（比如上次进程异常退出，销毁命令没来得及跑），直接 `docker run --name my-project-dev …` 会因**重名**报错、连带本次任务失败。想抗住这种残留，把启动命令写成先清理再创建：
>
> ```bash
> docker rm -f my-project-dev 2>/dev/null; docker run -d --name my-project-dev -v ~/workspace:/workspace A tail -f /dev/null
> ```

## 配置示例

```yaml
repos:
  - name: my-project
    aliases: [myproj]
    path: ~/workspace/my-project                 # 本地 git 仓库（同 local，必须含 .git）
    default_branch: main
    keywords: [my-project]
    mode: docker
    docker_container: my-project-dev             # 运行中的容器名 / ID（cc-fleet 只 exec 进去）
    # 可选：bind-mount 前缀对。不配则容器内路径与主机一致（同路径 bind-mount）。
    docker_host_root: ~/workspace                 # 主机被挂载的根
    docker_container_root: /workspace             # 它在容器内的挂载点
```

### 容器与挂载的前提

- 容器生命周期有两种管理方式（详见上方「[两种容器管理方式](#两种容器管理方式)」）：**不配** `docker_start_command` / `docker_stop_command` 时（方式一），容器须由用户预先起好并保持运行，cc-fleet 全程只 `docker exec`；**配齐**这对字段时（方式二），主控在每个任务开发前后自动起停容器。
- worktree 路径是 `<path>-worktrees/<slug>`（`path` 的兄弟目录）。容器的 bind-mount 必须**同时覆盖 `path` 与该 `-worktrees` 兄弟目录**——最简单的做法是挂它们的公共父目录（如上例 `~/workspace`），这样新 session 的 worktree 一旦在主机创建，容器内立即可见。
- 若主机挂载点与容器内路径**相同**（同路径 bind-mount），可省略 `docker_host_root` / `docker_container_root`；否则两者**必须同时配置**。

## 与 local 的差异

| 阶段 | local | docker |
|---|---|---|
| 创建 worktree | 主控在 `path` 下 `git worktree add`（主机本地） | **完全相同**（主机本地） |
| 改代码 / 读代码 | claude 在主机 worktree 内直接读写 | **完全相同**（主机本地） |
| 编译 / 测试 / 运行 | claude 在主机直接跑 | claude 经 `docker exec {docker_container} bash -lc 'cd <容器内worktree> && …'` 在容器内跑 |
| commit | claude 在主机 worktree `git commit` | **完全相同**（主机本地） |
| 发布（push + 建 MR/PR） | 主控本地直接提（`create_review_request`，按平台分流） | **完全相同**（主控本地直接提） |
| 代码审查（如启用） | 主控本地 `git diff` 交 Reviewer | **完全相同**（主机本地 `git diff`） |

实现上 `Session._is_remote()` 对 docker 返回 `False`，因此 docker **自动复用**所有 local 分支；唯一的差异是 dev 阶段渲染 `dev_protocol_docker.md` 而非 `dev_protocol_local.md`。

## 容器内 worktree 路径约定

dev 协议里 claude 用到的容器内路径由主控按前缀映射确定性算出：

- 配了前缀对：`container_worktree = docker_container_root + (host_worktree - docker_host_root)`
- 未配：`container_worktree = host_worktree`（同路径）

claude 在 `dev_protocol_docker.md` 里看到 `{container_worktree_path}` 占位被展开后的实际值，`docker exec ... cd <该路径>` 即对应主机 worktree。

## docker exec 约定与失败即阻塞

- 编译 / 测试 / 运行命令统一写成 `docker exec {docker_container} bash -lc '<命令>'`，**内层命令用单引号包裹**（与 remote 的 `ssh '…'` 同构，也让 PreToolUse 守卫稳妥放行）。
- `docker exec` 编译 / 测试**失败即当作阻塞**：claude 停下、原样贴出命令与报错，**不 commit 未通过编译的代码**，也不自行 `docker run / start` 拉起容器（容器起停归主控或用户管理，见「[两种容器管理方式](#两种容器管理方式)」，claude 在 dev 阶段不插手）。

## PreToolUse 白名单语义

- `CC_FLEET_WORKTREE`：主机 worktree（docker 模式 = 本地真实 worktree，同 local）。
- `CC_FLEET_EXTRA_WORKTREE_ROOTS`：docker 模式下，**当配了前缀映射、容器内路径与主机不同**时，主控把容器内 worktree 路径注入此处。
  - 注意：单引号包裹的 `docker exec <c> bash -lc '…'` 内层命令会被守卫的引号剥离逻辑整段去掉，本就不会被「工作目录外写入」误拦；此注入只是对**未加引号**的容器绝对路径 / `docker cp c:/path` 这类写法的兜底（belt-and-suspenders）。
- 拦 force push、拦敏感目录（`~/.ssh` / `~/.aws` / `/etc/passwd` 等）在 docker 模式下照常生效（包在 `docker exec '…'` 里也命中）。

## 平台分流

docker 模式的 worktree 是主机本地真实 git 仓库，`platform: auto` 会像 local 一样探测 worktree 的 origin remote URL（含 `github.com` → github，否则 gitlab）。GitHub Enterprise 自有域名探测不出，须显式写 `platform: github`。

## 相关源码

- `src/cc_fleet/core/session.py:_is_docker()` / `_container_worktree_path()` / `_format_docker_prompt()` —— docker 识别、容器路径换算、占位展开
- `src/cc_fleet/core/session.py:_ensure_docker_container()` / `_destroy_docker_container()` —— 方式二的自动起停（dev 前 inspect→起容器→轮询等 running，dev 后销毁）
- `src/cc_fleet/core/session.py:_render_dev_system_prompt_file()` —— dev prompt 按 mode 选模板
- `src/cc_fleet/core/session.py:_claude_extra_env()` —— docker 路径映射时注入容器 worktree 白名单
- `src/cc_fleet/config/schema.py` —— `mode=docker` 字段与校验（`_check_docker_fields` / `validate_runtime`）
- `src/cc_fleet/prompts/dev_protocol_docker.md` —— dev 协议（主机 commit + 容器编译 + 失败即阻塞）
