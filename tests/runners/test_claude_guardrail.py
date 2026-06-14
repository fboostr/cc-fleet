"""ClaudeGuardrailProvider.prepare：生成 settings.json + 返回 handle（P1 新增）。"""

from __future__ import annotations

import json
from pathlib import Path

from cc_fleet.core.runners.claude import ClaudeGuardrailProvider


def test_prepare_writes_settings_and_returns_handle(tmp_path: Path):
    gp = ClaudeGuardrailProvider()
    settings_dir = tmp_path / ".cc-fleet"
    handle = gp.prepare(settings_dir=settings_dir)

    # settings.json 落在 settings_dir 下并真实写出
    assert handle.settings_path == settings_dir / "settings.json"
    assert handle.settings_path.exists()

    # 内容含挂在 PreToolUse 上的命令钩子
    data = json.loads(handle.settings_path.read_text(encoding="utf-8"))
    assert "hooks" in data
    assert "PreToolUse" in data["hooks"]
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["type"] == "command"

    # claude 不用 cli 旗标护栏；白名单 env 仍由 session 经 extra_env 传，故此处为空
    assert handle.extra_cli_args == []
    assert handle.env == {}
