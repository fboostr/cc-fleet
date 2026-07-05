"""cli.py：argparse 分发、main 退出码、sessions list/logs、_dig 辅助。

不覆盖 wechat-login 的扫码轮询循环（纯 I/O 编排，依赖外部 ilink 端点 + 真实等待）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_fleet import cli
from cc_fleet.storage.db import Database


# ---------- _dig ----------

def test_dig_returns_first_nonempty():
    assert cli._dig({"a": "", "b": "x", "c": "y"}, "a", "b", "c") == "x"


def test_dig_missing_returns_empty():
    assert cli._dig({"a": ""}, "a", "z") == ""


def test_dig_coerces_to_str():
    assert cli._dig({"n": 5}, "n") == "5"


# ---------- build_parser ----------

def test_parser_run_defaults():
    ns = cli.build_parser().parse_args(["run"])
    assert ns.func is cli._cmd_run
    assert ns.config == "config.yaml"


def test_parser_global_config_flag():
    ns = cli.build_parser().parse_args(["--config", "x.yaml", "sessions", "list"])
    assert ns.config == "x.yaml"
    assert ns.func is cli._cmd_sessions_list


def test_parser_sessions_cancel_captures_slug():
    ns = cli.build_parser().parse_args(["sessions", "cancel", "abc"])
    assert ns.func is cli._cmd_sessions_cancel
    assert ns.slug == "abc"


def test_parser_sessions_logs_captures_slug():
    ns = cli.build_parser().parse_args(["sessions", "logs", "xyz"])
    assert ns.func is cli._cmd_sessions_logs
    assert ns.slug == "xyz"


def test_parser_wechat_login_base_url():
    p = cli.build_parser()
    assert p.parse_args(["wechat-login"]).base_url is None
    assert p.parse_args(["wechat-login", "--base-url", "https://h"]).base_url == "https://h"


def test_parser_requires_top_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_parser_requires_sessions_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sessions"])


# ---------- main 分发 / 退出码 ----------

def test_main_exits_with_func_return_code(monkeypatch):
    called = {}

    def stub(args):
        called["hit"] = True
        return 2

    monkeypatch.setattr(cli, "_cmd_sessions_list", stub)
    with pytest.raises(SystemExit) as ei:
        cli.main(["sessions", "list"])
    assert ei.value.code == 2
    assert called.get("hit")


def test_main_none_return_exits_zero(monkeypatch):
    monkeypatch.setattr(cli, "_cmd_sessions_list", lambda args: None)
    with pytest.raises(SystemExit) as ei:
        cli.main(["sessions", "list"])
    assert ei.value.code == 0


# ---------- sessions logs ----------

def _logs_ns(slug: str, *, raw: bool = False, tail: int = 0) -> SimpleNamespace:
    return SimpleNamespace(config="c", slug=slug, raw=raw, tail=tail)


def _write_session_dir(ws: Path, slug: str, fname: str, body: str) -> None:
    sdir = ws / "sessions" / slug
    sdir.mkdir(parents=True)
    (sdir / fname).write_text(body, encoding="utf-8")


def test_cmd_sessions_logs_prints_readable_log_full(monkeypatch, tmp_path: Path, capsys):
    ws = tmp_path / "ws"
    _write_session_dir(ws, "myslug", "session.log", "\n".join(f"line{i}" for i in range(250)))
    monkeypatch.setattr(cli, "_load", lambda args: SimpleNamespace(workspace_root=ws))
    rc = cli._cmd_sessions_logs(_logs_ns("myslug"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "line0" in out  # 默认打印全文，不再硬截末 200 行
    assert "line249" in out


def test_cmd_sessions_logs_tail_limits(monkeypatch, tmp_path: Path, capsys):
    ws = tmp_path / "ws"
    _write_session_dir(ws, "myslug", "session.log", "\n".join(f"line{i}" for i in range(250)))
    monkeypatch.setattr(cli, "_load", lambda args: SimpleNamespace(workspace_root=ws))
    rc = cli._cmd_sessions_logs(_logs_ns("myslug", tail=100))
    out = capsys.readouterr().out
    assert rc == 0
    assert "line249" in out
    assert "line150" in out
    assert "line149" not in out  # 只保留末 100 行（line150..line249）


def test_cmd_sessions_logs_raw_reads_stream_jsonl(monkeypatch, tmp_path: Path, capsys):
    ws = tmp_path / "ws"
    _write_session_dir(ws, "myslug", "stream.jsonl", '{"type":"result"}')
    monkeypatch.setattr(cli, "_load", lambda args: SimpleNamespace(workspace_root=ws))
    rc = cli._cmd_sessions_logs(_logs_ns("myslug", raw=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert '{"type":"result"}' in out


def test_cmd_sessions_logs_missing_file(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(cli, "_load", lambda args: SimpleNamespace(workspace_root=tmp_path))
    rc = cli._cmd_sessions_logs(_logs_ns("nope"))
    assert rc == 1
    assert "未找到" in capsys.readouterr().err


# ---------- sessions list ----------

async def test_list_sessions_renders_row(tmp_path: Path, capsys):
    dbpath = tmp_path / "state.db"
    db = Database(dbpath)
    await db.connect()
    await db.insert_session(
        {
            "slug": "tmp-abc",
            "repo": "demo",
            "state": "planning",
            "default_branch": "main",
            "initial_request": "做点什么",
        }
    )
    await db.close()

    await cli._list_sessions(SimpleNamespace(db_path=dbpath))
    out = capsys.readouterr().out
    assert "tmp-abc" in out
    assert "demo" in out
    assert "planning" in out


async def test_list_sessions_empty(tmp_path: Path, capsys):
    await cli._list_sessions(SimpleNamespace(db_path=tmp_path / "empty.db"))
    assert "暂无 session" in capsys.readouterr().out
