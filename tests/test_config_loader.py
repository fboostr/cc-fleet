"""config.loader：未选用平台段不参与 env 展开，选用平台段仍强校验。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cc_fleet.config.loader import load_config
from cc_fleet.config.schema import (
    ChatConfig,
    HttpConfig,
    PipelineConfig,
    PlatformType,
    StageTimeout,
)


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


def test_repo_base_remote_default_and_override(tmp_path):
    """RepoConfig.base_remote 缺省 'origin'（向后兼容），可覆盖为 'upstream'（fork 工作流）。"""
    from cc_fleet.config.schema import RepoConfig

    assert RepoConfig(name="a", path=tmp_path).base_remote == "origin"
    assert RepoConfig(name="b", path=tmp_path, base_remote="upstream").base_remote == "upstream"


def test_selected_platform_still_requires_its_env(tmp_path, monkeypatch):
    # 选用平台自身缺环境变量时仍应明确报错（没有被放松）
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.setenv("WECOM_BOT_SECRET", "sec-1")
    with pytest.raises(ValueError, match="WECOM_BOT_ID"):
        load_config(_write(tmp_path, "wecom"))


# ── /chat 配置段 ──────────────────────────────────────────────────────


def test_chat_section_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("WECOM_BOT_ID", "id-1")
    monkeypatch.setenv("WECOM_BOT_SECRET", "sec-1")
    body = textwrap.dedent(
        f"""
        workspace_root: {tmp_path}/ws
        log_dir: {tmp_path}/logs
        db_path: {tmp_path}/state.db
        platform: wecom
        wecom:
          bot_id: ${{env:WECOM_BOT_ID}}
          bot_secret: ${{env:WECOM_BOT_SECRET}}
        chat:
          default_cwd: {tmp_path}/chatdir
          max_concurrent: 2
          turn:
            idle_sec: 60
            tool_sec: 90
            hard_cap_sec: 120
        repos:
          - name: demo
            path: {tmp_path}
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.chat.max_concurrent == 2
    assert (cfg.chat.turn.idle_sec, cfg.chat.turn.tool_sec, cfg.chat.turn.hard_cap_sec) == (
        60,
        90,
        120,
    )
    assert str(cfg.chat.default_cwd) == f"{tmp_path}/chatdir"


def test_chat_section_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("WECOM_BOT_ID", "id-1")
    monkeypatch.setenv("WECOM_BOT_SECRET", "sec-1")
    cfg = load_config(_write(tmp_path, "wecom"))
    assert cfg.chat.default_cwd is None
    assert cfg.chat.max_concurrent == 4
    assert cfg.chat.turn.hard_cap_sec == 3600


# ── 超时结构化：三档约束 / 旧标量等价迁移（方案 C）/ 按仓库覆盖 ──────────
def test_stage_timeout_rejects_out_of_order():
    # idle ≤ tool ≤ hard_cap 被违反时报错（否则分档失去意义）
    with pytest.raises(ValueError, match="idle_sec ≤ tool_sec ≤ hard_cap_sec"):
        StageTimeout(idle_sec=100, tool_sec=10, hard_cap_sec=5)


def test_pipeline_legacy_scalar_migrated_equivalently(caplog):
    """方案 C：旧标量 ``dev_timeout_sec`` 等价迁移成 idle=tool=hard_cap（旧墙钟行为不变），
    并发一条中性 deprecation；其它未提及阶段仍用新默认。"""
    with caplog.at_level("WARNING"):
        pc = PipelineConfig(dev_timeout_sec=300)
    assert (pc.dev.idle_sec, pc.dev.tool_sec, pc.dev.hard_cap_sec) == (300, 300, 300)
    # plan 未给旧字段 → 新分档默认
    assert (pc.plan.idle_sec, pc.plan.hard_cap_sec) == (300, 3600)
    assert any("dev_timeout_sec" in r.getMessage() for r in caplog.records)


def test_chat_legacy_turn_timeout_migrated():
    cc = ChatConfig(turn_timeout_sec=120)
    assert (cc.turn.idle_sec, cc.turn.tool_sec, cc.turn.hard_cap_sec) == (120, 120, 120)


def test_legacy_and_new_same_stage_conflict_errors():
    # 同阶段旧字段 + 新字段并存 → 显式报错（不猜用户意图）
    with pytest.raises(ValueError, match="dev"):
        PipelineConfig(
            dev_timeout_sec=300, dev={"idle_sec": 1, "tool_sec": 2, "hard_cap_sec": 3}
        )


def test_repo_timeouts_override_resolution(tmp_path, monkeypatch):
    """AppConfig.stage_timeout：repo 级 timeouts 覆盖优先，未覆盖的阶段回退全局 pipeline。"""
    monkeypatch.setenv("WECOM_BOT_ID", "id-1")
    monkeypatch.setenv("WECOM_BOT_SECRET", "sec-1")
    body = textwrap.dedent(
        f"""
        workspace_root: {tmp_path}/ws
        log_dir: {tmp_path}/logs
        db_path: {tmp_path}/state.db
        platform: wecom
        wecom:
          bot_id: ${{env:WECOM_BOT_ID}}
          bot_secret: ${{env:WECOM_BOT_SECRET}}
        pipeline:
          dev:
            idle_sec: 600
            tool_sec: 3600
            hard_cap_sec: 28800
        repos:
          - name: heavy
            path: {tmp_path}
            timeouts:
              dev:
                idle_sec: 900
                tool_sec: 7200
                hard_cap_sec: 36000
          - name: light
            path: {tmp_path}
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    cfg = load_config(p)
    heavy = cfg.repo_by_name_or_alias("heavy")
    light = cfg.repo_by_name_or_alias("light")
    # heavy 覆盖了 dev → 用覆盖值
    assert cfg.stage_timeout(heavy, "dev").tool_sec == 7200
    # heavy 未覆盖 plan → 回退全局默认（hard_cap 3600）
    assert cfg.stage_timeout(heavy, "plan").hard_cap_sec == 3600
    # light 无任何覆盖 → 全局 dev（tool 3600）
    assert cfg.stage_timeout(light, "dev").tool_sec == 3600


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
