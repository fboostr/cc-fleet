"""security/permissions.py：PreToolUse hook 的 settings.json 生成与落盘。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cc_fleet.security import permissions


def test_hook_command_defaults_to_current_interpreter():
    cmd = permissions.hook_command()
    assert cmd == f"{sys.executable} -m cc_fleet.security.hooks.pretool_guard"


def test_hook_command_respects_env_override(monkeypatch):
    monkeypatch.setenv("CC_FLEET_HOOK_PYTHON", "/opt/py/bin/python")
    assert permissions.hook_command() == "/opt/py/bin/python -m cc_fleet.security.hooks.pretool_guard"


def test_render_settings_structure():
    s = permissions.render_settings()
    pre = s["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["matcher"] == ".*"
    hook = pre[0]["hooks"][0]
    assert hook["type"] == "command"
    assert "pretool_guard" in hook["command"]


def test_write_settings_creates_file_with_valid_json(tmp_path: Path):
    target = tmp_path / "nested" / ".cc-fleet"  # 父目录不存在，应被创建
    path = permissions.write_settings(target)
    assert path == target / "settings.json"
    assert path.exists()
    # 内容是合法 JSON 且与 render_settings 一致
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == permissions.render_settings()


def test_write_settings_expands_user(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = permissions.write_settings(Path("~/cfgdir"))
    assert path == home / "cfgdir" / "settings.json"
    assert path.exists()
