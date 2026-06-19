"""配置文件的 pydantic schema。"""

from __future__ import annotations

import ipaddress
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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
    """驱动 Coder / Reviewer 的 AI coding 工具。后续新增工具在此注册。"""

    CLAUDE = "claude"
    # 后续阶段在此注册：CODEX = "codex"、OPENCODE = "opencode"


class ClaudeConfig(BaseModel):
    """Claude Code 工具的专属配置块。

    与后续 ``CodexConfig`` / ``OpencodeConfig`` 对称——每个 coding agent 一个配置块，
    都至少有 ``binary``，各自再加工具专属项（如 codex 的 sandbox 档位、登录方式）。
    目前仅 ``binary`` 是对称占位，后续会被 claude 专属 flag（model、特定 env 等）填实，
    并非冗余。工具无关的阶段超时 / 澄清轮次见 ``PipelineConfig``。
    """

    binary: str = "claude"


class PipelineConfig(BaseModel):
    """交付流水线的阶段参数（工具无关）。

    plan / dev / review 是状态机的阶段，这些超时与澄清轮次上限对所有 coding agent 通用，
    故独立于具体工具的配置块（``ClaudeConfig`` 等），不随工具重复——这也是「编排层工具
    无关、工具耦合只在 runner 层」在配置层的体现。
    """

    plan_timeout_sec: int = 1800
    dev_timeout_sec: int = 3600
    # Reviewer 单次审查（plan / code review 共用）的超时秒数。审查是只读分析，
    # 时长与 plan 阶段相当，故默认与 plan_timeout_sec 一致。
    review_timeout_sec: int = 1800
    max_clarify_rounds: int = 5


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
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    repos: list[RepoConfig]
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
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

    def validate_runtime(self) -> list[str]:
        """启动期对每个 repo 做静态校验，把"配置漏改"在拉起进程前就拦住。

        - mode=local 的 path 必须存在，且看起来是 git 仓库（含 `.git` 目录或文件）
        - mode=remote 的 path 必须存在（壳子目录）

        返回错误清单（空表示通过）；caller 用它决定是否拒绝启动。
        """
        errs: list[str] = []
        for r in self.repos:
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
