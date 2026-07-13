"""配置文件的 pydantic schema。"""

from __future__ import annotations

import ipaddress
import logging
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


class PlatformType(str, Enum):
    """聊天平台类型。后续新增平台在此注册。"""

    WECOM = "wecom"
    WECHAT = "wechat"


class WecomConfig(BaseModel):
    bot_id: str
    bot_secret: str
    allowed_chatids: list[str] = Field(default_factory=list)


class WechatConfig(BaseModel):
    """个人微信（ilink ClawBot）机器人凭据。

    - bot_token：扫码登录后拿到的长期 token（用 `cc-fleet wechat-login` 取，建议走环境变量注入）
    - base_url：ilink 官方端点；扫码登录返回的 baseurl 若不同应以其为准
    - allowed_user_ids：仅允许这些 from_user_id 触发；为空表示不限制
    """

    bot_token: str
    base_url: str = "https://ilinkai.weixin.qq.com"
    allowed_user_ids: list[str] = Field(default_factory=list)


class AgentTool(str, Enum):
    """驱动 Coder / Reviewer 的 AI coding 工具。后续新增工具在此注册。

    枚举值存在 ≠ runner 已接入：以 ``core/runner_factory.py`` 的 ``SUPPORTED_TOOLS``
    为准，配置引用未接入的工具会在 ``AppConfig.validate_runtime`` 被启动期拦下。
    """

    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"


class ClaudeConfig(BaseModel):
    """Claude Code 工具的专属配置块。

    与 ``CodexConfig`` / ``OpencodeConfig`` 对称——每个 coding agent 一个配置块，
    都至少有 ``binary``，各自再加工具专属项。工具无关的阶段超时 / 澄清轮次见
    ``PipelineConfig``。
    """

    binary: str = "claude"


class CodexConfig(BaseModel):
    """Codex CLI 工具的专属配置块（与 ``ClaudeConfig`` 对称）。

    runner 接入前仅是配置插槽：``agent: codex`` 会被 ``validate_runtime``
    启动期拦下，直到 ``core/runner_factory.py`` 加上对应分支。
    """

    binary: str = "codex"
    # 传给 codex 的模型名（--model）；None 用 codex 自身默认。
    model: str | None = None


class OpencodeConfig(BaseModel):
    """opencode 工具的专属配置块（与 ``ClaudeConfig`` 对称）。

    runner 接入前仅是配置插槽，同 ``CodexConfig``。
    """

    binary: str = "opencode"
    # 传给 opencode 的模型名；None 用 opencode 自身默认。
    model: str | None = None


class StageTimeout(BaseModel):
    """单个阶段的空闲超时策略（一一对应 runner 层的 ``TimeoutPolicy``）。

    不再用「进程总时长」一刀切，而是按「是否有工具在飞」分三档（详见
    ``core/runners/base.py`` 的 ``TimeoutPolicy``）：

    - ``idle_sec``：无工具在飞时的「回合间空闲」上限（真·卡死 / 等输入信号，收得紧）。
    - ``tool_sec``：有工具在飞（``tool_use`` 已发、``tool_result`` 未回）时的静默上限，
      覆盖大型 C++ 编译 / 数十分钟测试等合法长工具（放得松）。
    - ``hard_cap_sec``：从进程启动起算的绝对总时长兜底，防「一直吐事件的病态死循环」。

    约束 ``idle_sec ≤ tool_sec ≤ hard_cap_sec``，否则分档失去意义（如 idle > hard_cap
    时空闲档永不触发）。
    """

    idle_sec: int = Field(default=300, gt=0)
    tool_sec: int = Field(default=900, gt=0)
    hard_cap_sec: int = Field(default=3600, gt=0)

    @model_validator(mode="after")
    def _check_order(self) -> "StageTimeout":
        if not (self.idle_sec <= self.tool_sec <= self.hard_cap_sec):
            raise ValueError(
                "超时三档需满足 idle_sec ≤ tool_sec ≤ hard_cap_sec，当前 "
                f"{self.idle_sec}/{self.tool_sec}/{self.hard_cap_sec}"
            )
        return self

    def to_policy(self) -> "TimeoutPolicy":
        """转成 runner 层的 ``TimeoutPolicy``（方法内延迟 import，避免 config→runners 的模块级依赖环）。"""
        from ..core.runners.base import TimeoutPolicy

        return TimeoutPolicy(
            idle_sec=self.idle_sec, tool_sec=self.tool_sec, hard_cap_sec=self.hard_cap_sec
        )


def _default_stage(idle: int, tool: int, hard_cap: int):
    """给 ``Field(default_factory=...)`` 用的工厂：产出一个指定三档默认值的 ``StageTimeout``。"""
    return lambda: StageTimeout(idle_sec=idle, tool_sec=tool, hard_cap_sec=hard_cap)


# 结构化改造前的单标量超时字段 → 新结构里对应的阶段键。旧配置里出现这些标量时**不报错**，
# 而是等价迁移（方案 C）：旧标量语义是「墙钟总上限」，映射为 idle=tool=hard_cap=旧值——
# 旧一刀切行为逐字节不变，只换内部表达。不擅自放宽 idle/tool（那会改变旧超时行为）；想要
# 空闲分档得显式改用新结构。pipeline 与 chat.turn 共用。
_LEGACY_TIMEOUT_STAGE = {
    "plan_timeout_sec": "plan",
    "dev_timeout_sec": "dev",
    "review_timeout_sec": "review",
    "turn_timeout_sec": "turn",
}


def _migrate_legacy_timeouts(data, where: str, allowed: set[str]):
    """把旧标量超时字段等价迁移成新的 ``StageTimeout`` dict（方案 C：三档全相等，行为不变）。

    ``allowed`` 限定本段接受哪些旧键（pipeline 收 plan/dev/review_timeout_sec，chat 收
    turn_timeout_sec）。返回处理后的**新** data（复制，不原地改调用方 dict）；``data`` 非
    dict 时原样返回。同一阶段旧字段与新字段并存视为冲突、报错；命中旧字段时发一条中性
    deprecation（每 key 一次），提示想要空闲分档需改用新结构。
    """
    if not isinstance(data, dict):
        return data
    data = dict(data)
    for old_key in allowed:
        if old_key not in data:
            continue
        stage = _LEGACY_TIMEOUT_STAGE[old_key]
        val = data.pop(old_key)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise ValueError(f"{where} 段的 {old_key} 需为正整数秒数，当前 {val!r}")
        if stage in data:
            raise ValueError(
                f"{where} 段同时给了旧字段 {old_key} 与新字段 {stage}，请二选一"
                "（新结构见 config.example.yaml）"
            )
        logger.warning(
            "检测到 %s 段旧格式 %s，已按等价墙钟迁移（行为不变）；"
            "若想让长编译 / 测试吃到空闲分档，请改用新结构 %s: {idle_sec/tool_sec/hard_cap_sec}。",
            where,
            old_key,
            stage,
        )
        data[stage] = {"idle_sec": val, "tool_sec": val, "hard_cap_sec": val}
    return data


class PipelineConfig(BaseModel):
    """交付流水线的阶段参数（工具无关）。

    plan / dev / review 是状态机的阶段，这些超时与澄清轮次上限对所有 coding agent 通用，
    故独立于具体工具的配置块（``ClaudeConfig`` 等），不随工具重复——这也是「编排层工具
    无关、工具耦合只在 runner 层」在配置层的体现。

    每阶段一个 ``StageTimeout``，默认按阶段分档：dev 放宽（长编译 / 测试），plan / review
    收紧（只读分析 / 探索，鲜有长工具）。可被 ``RepoConfig.timeouts`` 按仓库覆盖。
    """

    plan: StageTimeout = Field(default_factory=_default_stage(300, 900, 3600))
    dev: StageTimeout = Field(default_factory=_default_stage(600, 3600, 28800))
    review: StageTimeout = Field(default_factory=_default_stage(300, 900, 3600))
    max_clarify_rounds: int = 5

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data):
        return _migrate_legacy_timeouts(
            data, "pipeline", {"plan_timeout_sec", "dev_timeout_sec", "review_timeout_sec"}
        )


class PipelineTimeoutOverride(BaseModel):
    """``RepoConfig`` 级的超时覆盖：每阶段可选整块替换全局 ``pipeline`` 默认，未填的阶段回退全局。"""

    plan: StageTimeout | None = None
    dev: StageTimeout | None = None
    review: StageTimeout | None = None


class ReviewerConfig(BaseModel):
    """每个 repo 的独立 Reviewer 开关（默认关闭）。

    Reviewer 是独立于 Coder 的第二个 LLM agent：plan 阶段审查 plan、dev 阶段审查代码，
    Coder 据其意见完善。详见 core/session.py 的 _do_plan_reviewing / _do_code_reviewing。

    - enabled：是否启用。默认 False，关闭时行为与无 Reviewer 完全一致。
    - max_rounds：「审查→Coder 修订」的轮次上限（plan 与 code 各自独立计数）。
      默认 1（审 1 次、修 1 次即放行）；0 等价于关闭。

    远期会支持 Reviewer 用不同的 AI 工具 / 大模型，预留在本结构内扩展（如 model / tool 字段）。
    """

    enabled: bool = False
    max_rounds: int = 1
    # 预留：Reviewer 用与 Coder 不同的 AI 工具 / 大模型（None = 跟随 repo.agent）。
    # P1 仅声明字段、不接线；跨工具审查待后续阶段工具就位后实现。
    tool: AgentTool | None = None


class RepoConfig(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    path: Path
    default_branch: str = "main"
    # base 远端：fetch base 分支、建 worktree、算「领先几个 commit」、定 MR/PR 目标都用
    # {base_remote}/{default_branch}。默认 "origin"（同仓库直推：从 origin 起 base、合回
    # origin，与改动前完全一致）。**fork / 跨仓库工作流**配成 "upstream"：从上游起 base，
    # 而 push 仍走 origin（你的 fork）——因为合并落在 upstream，只 fetch origin 会漏掉已合入
    # 上游的提交。该远端名须在本地主 clone（local）/ 远端 dev box（remote）里预先
    # `git remote add <base_remote> <url>` 配好；cc-fleet 只用其名，不负责创建。
    base_remote: str = "origin"
    keywords: list[str] = Field(default_factory=list)

    # 仓库工作模式：
    # - local：path 是本地 git 仓库，主控建 worktree、主控提 MR
    # - remote：path 仅是 claude code 启动壳子目录（**不必是 git 仓库**），
    #           真正代码与 worktree 都在远端 dev box；claude 自己 ssh 过去开发与提 MR
    mode: Literal["local", "remote"] = "local"

    # 代码托管平台，决定提 MR/PR 的方式：
    # - auto（默认）：mode=local 时按 origin remote URL 自动探测（含 github.com → github，
    #                 否则 gitlab）；mode=remote 时本地无 origin 可探测，回退 gitlab
    # - gitlab：走 `git push -o merge_request.create`（详见 core/mr.py）
    # - github：先普通 push 再调 GitHub REST API 建 PR，需在 .env 配 GITHUB_TOKEN / GH_TOKEN
    # 自建 GitHub Enterprise（非 github.com 域名）auto 探测识别不出，须显式写 github。
    platform: Literal["auto", "gitlab", "github"] = "auto"

    # mode=remote 时必填（model_validator 校验）
    remote_ssh_alias: str | None = None
    remote_repo_path: str | None = None
    remote_worktree_root: str | None = None

    # 可选：按仓库覆盖全局 pipeline 的阶段超时（重型仓库如大型 C++ 编译 / 测试可单独调大
    # dev.tool_sec）。只覆盖填了的阶段，其余回退全局 ``pipeline``；解析见 ``AppConfig.stage_timeout``。
    timeouts: PipelineTimeoutOverride | None = None

    # 驱动本 repo 的 AI coding 工具（Coder）。默认 claude，旧配置零感知、向后兼容。
    agent: AgentTool = AgentTool.CLAUDE

    # 独立 Reviewer 开关（默认关闭）。启用后：plan 审查 local+remote 都做；
    # code 审查仅 local（remote 模式 Coder 在 dev 阶段已 push 建 MR，无处插入 code 审查）。
    reviewer: ReviewerConfig = Field(default_factory=ReviewerConfig)

    @field_validator("path", mode="before")
    @classmethod
    def _expand(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve() if v else v

    @model_validator(mode="after")
    def _check_remote_fields(self) -> "RepoConfig":
        if self.mode == "remote":
            missing = [
                k
                for k in ("remote_ssh_alias", "remote_repo_path", "remote_worktree_root")
                if not (getattr(self, k) or "").strip()
            ]
            if missing:
                raise ValueError(
                    f"repo {self.name!r} mode=remote 但缺少字段：{', '.join(missing)}"
                )
        return self


class LimitsConfig(BaseModel):
    max_concurrent_sessions: int = 4


class HttpConfig(BaseModel):
    """本地只读 HTTP 面板配置。默认绑 127.0.0.1，避免无意暴露。"""

    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = 8787

    @field_validator("bind")
    @classmethod
    def _validate_bind(cls, v: str) -> str:
        v = v.strip()
        # 允许合法的 IPv4 / IPv6 地址
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            pass
        # 也允许主机名（如 localhost），但拒绝明显非法的格式：
        # 冒号分隔 + 恰好 4 段 + 每段纯数字 → 看起来像 IPv4 打错了分隔符（如 0:0:0:0）
        if ":" in v:
            parts = v.split(":")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                raise ValueError(
                    f"bind 地址 '{v}' 看起来像 IPv4 但用了冒号分隔，"
                    f"你可能想写的是 '{v.replace(':', '.')}'？"
                )
        return v


class ChatConfig(BaseModel):
    """`/chat` 自由对话通道的配置（独立于 plan→dev→MR 交付流水线）。

    chat 是只读的需求讨论：绑定了仓库时直接在仓库主目录（``repo.path``）以只读权限跑，
    不建 worktree、不改代码。

    - default_cwd：`/chat` 未带 `@repo`（且非单仓库自动绑定）时的回退工作目录；为空则
      回退到用户 home。绑定了仓库时忽略本项，cwd 用仓库主目录。
    - max_concurrent：并发 chat 轮次上限。独立于 ``limits.max_concurrent_sessions``，
      chat 常长时间挂着等用户，用独立池避免饿死交付流水线。
    - turn：单轮 claude 子进程的空闲超时策略（``StageTimeout``，三档语义同 pipeline 各阶段）。
      chat 是只读讨论、鲜有长工具，默认收紧。
    - auto_continue_window_sec：私聊「窗口内免引用自动续聊」时长（秒），默认 1800（30 分钟）、
      默认开启。仅在 ``default_mode='chat'`` 且**私聊**（无群概念）下生效：不带引用的普通消息，
      若该用户存在一个活跃 chat 会话、且距其**最后一条机器人回复** ≤ 本值，则自动续到该会话
      而非开新会话；超窗 / 无活跃 chat 则照旧开新。设 ``0`` 关闭（回到"必须引用才续聊"的旧行为）。
      刻意开新话题用 ``/chat <消息>``（永远开新，不受本项影响）。
    """

    default_cwd: Path | None = None
    max_concurrent: int = 4
    turn: StageTimeout = Field(default_factory=_default_stage(300, 600, 3600))
    auto_continue_window_sec: int = 1800

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data):
        return _migrate_legacy_timeouts(data, "chat", {"turn_timeout_sec"})

    @field_validator("default_cwd", mode="before")
    @classmethod
    def _expand_cwd(cls, v: str | Path | None) -> Path | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return Path(v).expanduser()


class AppConfig(BaseModel):
    workspace_root: Path
    log_dir: Path
    db_path: Path

    # 聊天平台选择：支持 wecom（企业微信）/ wechat（个人微信 ilink）。
    # 默认 wecom，向后兼容旧配置。
    platform: PlatformType = PlatformType.WECOM

    wecom: WecomConfig | None = None
    wechat: WechatConfig | None = None
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    opencode: OpencodeConfig = Field(default_factory=OpencodeConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    repos: list[RepoConfig]

    # 无命令、无引用、无显式 @repo 的普通消息，默认按哪种模式处理（用于降低新手门槛）：
    # - chat（默认）：进入 /chat 多轮讨论，聊清楚后引用消息发 /dev 转正式开发；更贴合普通用户
    #   "先聊几轮把需求讲明白"的习惯。
    # - dev：直接进入 plan→dev→MR 交付流水线（等价旧默认行为），适合"一句话就是明确需求"的老手。
    # 无论此项为何，`@<repo> /dev <需求>` 与「引用一条 /chat 消息 + /dev」都能直达开发，不受影响。
    default_mode: Literal["chat", "dev"] = "chat"

    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    worktree_retention_hours: int = 168

    @model_validator(mode="before")
    @classmethod
    def _reject_migrated_claude_fields(cls, data):
        """阶段超时 / 澄清轮次已从 claude 段迁到 pipeline 段；旧配置残留时显式报错，
        避免 pydantic 默认忽略未知字段导致旧超时被静默吞掉。"""
        if isinstance(data, dict):
            claude_raw = data.get("claude")
            if isinstance(claude_raw, dict):
                moved = {
                    "plan_timeout_sec",
                    "dev_timeout_sec",
                    "review_timeout_sec",
                    "max_clarify_rounds",
                }
                hit = sorted(moved & set(claude_raw))
                if hit:
                    raise ValueError(
                        f"claude 段的 {hit} 已迁移到 pipeline 段；请改写为 "
                        "pipeline: {plan_timeout_sec / dev_timeout_sec / "
                        "review_timeout_sec / max_clarify_rounds}"
                    )
        return data

    @model_validator(mode="after")
    def _check_platform_config(self) -> "AppConfig":
        if self.platform == PlatformType.WECOM and self.wecom is None:
            raise ValueError(
                "platform=wecom 要求在配置中提供 'wecom' 段"
            )
        if self.platform == PlatformType.WECHAT and self.wechat is None:
            raise ValueError(
                "platform=wechat 要求在配置中提供 'wechat' 段"
            )
        return self

    @field_validator("workspace_root", "log_dir", "db_path", mode="before")
    @classmethod
    def _expand_path(cls, v: str | Path) -> Path:
        return Path(v).expanduser()

    def repo_by_name_or_alias(self, key: str) -> RepoConfig | None:
        key_lower = key.lower()
        for repo in self.repos:
            if repo.name.lower() == key_lower:
                return repo
            if key_lower in (a.lower() for a in repo.aliases):
                return repo
        return None

    def agent_config(self, tool: AgentTool) -> ClaudeConfig | CodexConfig | OpencodeConfig:
        """按工具取对应的配置块（claude → ``self.claude`` 等），供 runner 工厂统一取用。"""
        blocks: dict[AgentTool, ClaudeConfig | CodexConfig | OpencodeConfig] = {
            AgentTool.CLAUDE: self.claude,
            AgentTool.CODEX: self.codex,
            AgentTool.OPENCODE: self.opencode,
        }
        return blocks[tool]

    def stage_timeout(
        self, repo: RepoConfig, stage: Literal["plan", "dev", "review"]
    ) -> StageTimeout:
        """解析某仓库某阶段的有效超时：repo 级 ``timeouts`` 覆盖优先，否则回退全局 ``pipeline``。"""
        override = repo.timeouts
        if override is not None:
            st = getattr(override, stage)
            if st is not None:
                return st
        return getattr(self.pipeline, stage)

    def validate_runtime(self) -> list[str]:
        """启动期对每个 repo 做静态校验，把"配置漏改"在拉起进程前就拦住。

        - mode=local 的 path 必须存在，且看起来是 git 仓库（含 `.git` 目录或文件）
        - mode=remote 的 path 必须存在（壳子目录）
        - agent / reviewer.tool 引用的工具必须已接入 runner（枚举存在但工厂无分支时
          在此拦下，而不是等运行到工厂才抛 ``ValueError``）

        返回错误清单（空表示通过）；caller 用它决定是否拒绝启动。
        """
        # 延迟 import 避免 config→core 的模块级依赖环（同 StageTimeout.to_policy 先例）。
        from ..core.runner_factory import SUPPORTED_TOOLS

        supported = "、".join(sorted(t.value for t in SUPPORTED_TOOLS))
        errs: list[str] = []
        for r in self.repos:
            for field_name, tool in (("agent", r.agent), ("reviewer.tool", r.reviewer.tool)):
                if tool is not None and tool not in SUPPORTED_TOOLS:
                    errs.append(
                        f"repo {r.name!r}: {field_name}={tool.value} 的 runner 尚未接入"
                        f"（当前支持：{supported}）"
                    )
            if not r.path.exists():
                errs.append(f"repo {r.name!r}: path {r.path} 不存在")
                continue
            if r.mode == "local" and not (r.path / ".git").exists():
                hint = (
                    "若代码在远端，请改为 mode: remote 并配"
                    " remote_ssh_alias / remote_repo_path / remote_worktree_root。"
                )
                errs.append(
                    f"repo {r.name!r}: mode={r.mode} 但 {r.path} 不是 git 仓库"
                    f"（缺少 .git）。{hint}"
                )
        return errs
