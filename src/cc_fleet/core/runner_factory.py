"""按工具选 runner 的工厂。

``get_runner(tool, config)`` 把 ``RepoConfig.agent`` 映射到具体 ``AgentRunner`` 实现。
后续阶段在此加分支接新工具 —— 纯加法，不碰已有代码。
"""

from __future__ import annotations

from ..config.schema import AgentTool, AppConfig, CodexConfig
from .runners.base import AgentRunner
from .runners.claude import ClaudeRunner
from .runners.codex import CodexRunner

# 已接入 runner 的工具（单一事实源）：``AgentTool`` 枚举值存在但不在此集合时，
# ``AppConfig.validate_runtime`` 会在启动期把配置拦下。接入新工具 = 加分支 + 入集合。
SUPPORTED_TOOLS: frozenset[AgentTool] = frozenset({AgentTool.CLAUDE, AgentTool.CODEX})


def get_runner(tool: AgentTool, config: AppConfig) -> AgentRunner:
    """按工具返回对应的 runner 实例。未接入的工具抛 ``ValueError``。"""
    if tool is AgentTool.CLAUDE:
        return ClaudeRunner(binary=config.agent_config(tool).binary)
    if tool is AgentTool.CODEX:
        cfg = config.agent_config(tool)
        assert isinstance(cfg, CodexConfig)
        return CodexRunner(cfg)
    raise ValueError(f"unsupported agent tool: {tool!r}")
