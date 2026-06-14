"""按工具选 runner 的工厂。

``get_runner(tool, config)`` 把 ``RepoConfig.agent`` 映射到具体 ``AgentRunner`` 实现。
P1 只有 claude；后续阶段在此加分支接 codex / opencode —— 纯加法，不碰已有代码。
"""

from __future__ import annotations

from ..config.schema import AgentTool, AppConfig
from .runners.base import AgentRunner
from .runners.claude import ClaudeRunner


def get_runner(tool: AgentTool, config: AppConfig) -> AgentRunner:
    """按工具返回对应的 runner 实例。未知工具抛 ``ValueError``。"""
    if tool is AgentTool.CLAUDE:
        return ClaudeRunner(binary=config.claude.binary)
    raise ValueError(f"unsupported agent tool: {tool!r}")
