"""config.loader：未选用平台段不参与 env 展开，选用平台段仍强校验。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cc_fleet.config.loader import load_config
from cc_fleet.config.schema import HttpConfig, PlatformType


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    # 避免读到工作目录里真实 .env 干扰用例
    monkeypatch.setattr("cc_fleet.config.loader.load_dotenv", lambda *a, **k: None)


def _write(tmp_path: Path, platform: str) -> Path:
    body = textwrap.dedent(
        f"""
        workspace_root: {tmp_path}/ws
        log_dir: {tmp_path}/logs
        db_path: {tmp_path}/state.db
        platform: {platform}
        wecom:
          bot_id: ${{env:WECOM_BOT_ID}}
          bot_secret: ${{env:WECOM_BOT_SECRET}}
        wechat:
          bot_token: ${{env:WECHAT_BOT_TOKEN}}
        repos:
          - name: demo
            path: {tmp_path}
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_unselected_wecom_section_dropped(tmp_path, monkeypatch):
    # 选 wechat：未使用的 wecom 段应被丢弃，不要求 WECOM_*
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.delenv("WECOM_BOT_SECRET", raising=False)
    monkeypatch.setenv("WECHAT_BOT_TOKEN", "tok-123")
    cfg = load_config(_write(tmp_path, "wechat"))
    assert cfg.platform == PlatformType.WECHAT
    assert cfg.wecom is None
    assert cfg.wechat is not None and cfg.wechat.bot_token == "tok-123"


def test_unselected_wechat_section_dropped(tmp_path, monkeypatch):
    # 选 wecom：未使用的 wechat 段应被丢弃，不要求 WECHAT_BOT_TOKEN
    monkeypatch.delenv("WECHAT_BOT_TOKEN", raising=False)
    monkeypatch.setenv("WECOM_BOT_ID", "id-1")
    monkeypatch.setenv("WECOM_BOT_SECRET", "sec-1")
    cfg = load_config(_write(tmp_path, "wecom"))
    assert cfg.platform == PlatformType.WECOM
    assert cfg.wechat is None
    assert cfg.wecom is not None and cfg.wecom.bot_id == "id-1"


def test_selected_platform_still_requires_its_env(tmp_path, monkeypatch):
    # 选用平台自身缺环境变量时仍应明确报错（没有被放松）
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.setenv("WECOM_BOT_SECRET", "sec-1")
    with pytest.raises(ValueError, match="WECOM_BOT_ID"):
        load_config(_write(tmp_path, "wecom"))


# ── HttpConfig.bind 格式校验 ──────────────────────────────────────────


class TestHttpConfigBind:
    """HttpConfig._validate_bind：IP 地址 / 主机名格式校验与笔误拦截。"""

    def test_valid_ipv4(self):
        cfg = HttpConfig(bind="0.0.0.0")
        assert cfg.bind == "0.0.0.0"

    def test_valid_ipv6(self):
        cfg = HttpConfig(bind="::1")
        assert cfg.bind == "::1"

    def test_valid_hostname(self):
        cfg = HttpConfig(bind="localhost")
        assert cfg.bind == "localhost"

    def test_colon_typo_rejected(self):
        with pytest.raises(ValueError, match="0.0.0.0"):
            HttpConfig(bind="0:0:0:0")

    def test_colon_typo_192_168_rejected(self):
        with pytest.raises(ValueError, match="192.168.1.1"):
            HttpConfig(bind="192:168:1:1")
