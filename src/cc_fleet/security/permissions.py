"""为每次 claude 调用动态生成 settings.json，把 PreToolUse hook 注进去。

claude 的 hook 配置长这样：
{
  "hooks": {
    "PreToolUse": [
      {"matcher": ".*", "hooks": [{"type":"command","command":"<解释器> <脚本>"}]}
    ]
  }
}

settings.json 落在 `<worktree>/.cc-fleet/settings.json`，
启动 claude 时通过 `--settings <path>` 传入。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import hooks


def hook_command() -> str:
    """组装 PreToolUse hook 的可执行命令字符串。

    `<venv 中的 python> -m cc_fleet.security.hooks.pretool_guard`
    通过 -m 启动既可携带项目所有依赖，又不需要把脚本路径硬编码。
    """
    python = os.environ.get("CC_FLEET_HOOK_PYTHON") or sys.executable
    return f"{python} -m cc_fleet.security.hooks.pretool_guard"


def render_settings() -> dict:
    """生成 settings.json 的内容（dict）。"""
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [
                        {"type": "command", "command": hook_command()},
                    ],
                }
            ]
        }
    }


def write_settings(target_dir: Path) -> Path:
    """把 settings.json 写到 target_dir / settings.json，返回完整路径。"""
    target_dir = target_dir.expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "settings.json"
    path.write_text(
        json.dumps(render_settings(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def main() -> None:
    """CLI：渲染 settings.json 到 stdout（便于手工验证 hook 是否正确接入）。"""
    json.dump(render_settings(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


# 保留对 hooks 包的引用以避免被 ruff 误判为 unused（导入它意在确保打包时包含）
_ = hooks


if __name__ == "__main__":
    main()
