"""``core/session_log`` 渲染器与可读日志写入器单测。

render_event 是纯函数：claude stream-json 事件 → 人类可读行（不含时间戳）；
噪声事件返回 []。SessionLogWriter 把渲染结果带时间戳追加进 session.log，
写失败降级为 warning，绝不抛。
"""

from __future__ import annotations

import pytest

from cc_fleet.core.session_log import SessionLogWriter, render_event


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _user_result(content, is_error: bool = False, tuid: str = "toolu_x") -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tuid,
                    "content": content,
                    "is_error": is_error,
                }
            ]
        },
    }


# ── render_event：assistant 内容块 ───────────────────────────────


def test_assistant_text_rendered():
    lines = render_event(_assistant({"type": "text", "text": "我先读一下代码"}))
    assert len(lines) == 1
    assert "我先读一下代码" in lines[0]


def test_tool_use_bash_shows_full_command():
    evt = _assistant(
        {
            "type": "tool_use",
            "name": "Bash",
            "input": {"command": 'grep -rn "用法：/plan" src/ 2>/dev/null', "description": "查"},
        }
    )
    lines = render_event(evt)
    assert len(lines) == 1
    assert "Bash" in lines[0]
    assert 'grep -rn "用法：/plan" src/ 2>/dev/null' in lines[0]


def test_tool_use_read_shows_file_path():
    evt = _assistant(
        {"type": "tool_use", "name": "Read", "input": {"file_path": "src/cc_fleet/core/dispatcher.py"}}
    )
    lines = render_event(evt)
    assert "Read" in lines[0]
    assert "src/cc_fleet/core/dispatcher.py" in lines[0]


def test_tool_use_write_shows_path_and_size_not_full_content():
    big = "x" * 5000
    evt = _assistant(
        {"type": "tool_use", "name": "Write", "input": {"file_path": "a.py", "content": big}}
    )
    lines = render_event(evt)
    assert "a.py" in lines[0]
    assert big not in lines[0]  # 不整段吐 content
    assert "5000" in lines[0]  # 显示字符数


def test_multiple_blocks_render_in_order():
    evt = _assistant(
        {"type": "text", "text": "先说明"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    )
    lines = render_event(evt)
    assert len(lines) == 2
    assert "先说明" in lines[0]
    assert "ls" in lines[1]


# ── render_event：tool_result ────────────────────────────────────


def test_tool_result_ok():
    lines = render_event(_user_result("hello world", is_error=False))
    assert len(lines) == 1
    assert "hello world" in lines[0]
    assert "⛔" not in lines[0]


def test_tool_result_error_guard_block_visible():
    lines = render_event(_user_result("禁止在工作目录外写入：/dev/null", is_error=True))
    assert len(lines) == 1
    assert "⛔" in lines[0]
    assert "禁止在工作目录外写入：/dev/null" in lines[0]


def test_tool_result_content_as_list_of_blocks():
    lines = render_event(_user_result([{"type": "text", "text": "块文本"}]))
    assert "块文本" in lines[0]


def test_large_field_truncated_with_marker():
    big = "行\n" * 4000  # 8000 字符，远超默认阈值
    lines = render_event(_user_result(big))
    joined = "\n".join(lines)
    assert "已省略" in joined
    assert "stream.jsonl" in joined
    assert len(joined) < len(big)


# ── render_event：result 终态 ────────────────────────────────────


def test_result_event_shows_is_error_and_turns():
    evt = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 87,
        "result": "最终总结",
    }
    lines = render_event(evt)
    assert len(lines) == 1
    assert "is_error" in lines[0]
    assert "87" in lines[0]


def test_system_init_summarized():
    evt = {
        "type": "system",
        "subtype": "init",
        "model": "claude-opus-4-8[1m]",
        "cwd": "/wt",
        "tools": ["Bash", "Read"],
    }
    lines = render_event(evt)
    assert len(lines) == 1
    assert "claude-opus-4-8[1m]" in lines[0]


# ── render_event：噪声/未知 → [] ────────────────────────────────


@pytest.mark.parametrize(
    "evt",
    [
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "system", "subtype": "status", "status": "requesting"},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50},
        {"type": "system", "subtype": "hook_started"},
        {"type": "system", "subtype": "hook_response", "output": "x"},
        {"type": "rate_limit_event", "rate_limit_info": {}},
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "…"}]}},
        {},
        {"type": "unknown_future_type"},
    ],
)
def test_noise_and_unknown_events_render_nothing(evt):
    assert render_event(evt) == []


# ── SessionLogWriter ─────────────────────────────────────────────


def test_writer_appends_rendered_event_with_timestamp(tmp_path):
    p = tmp_path / "sub" / "session.log"  # 父目录不存在 → 自动建
    w = SessionLogWriter(p)
    w.write_event(_assistant({"type": "text", "text": "第一句"}))
    w.write_event(_user_result("禁止在工作目录外写入：/dev/null", is_error=True))
    text = p.read_text(encoding="utf-8")
    assert "第一句" in text
    assert "⛔" in text
    assert "禁止在工作目录外写入：/dev/null" in text
    assert text.lstrip().startswith("[")  # 时间戳前缀


def test_writer_phase_and_note(tmp_path):
    p = tmp_path / "session.log"
    w = SessionLogWriter(p)
    w.write_phase("DEVELOPING")
    w.write_note("❌ 失败：dev 阶段结束但 worktree 无新 commit")
    text = p.read_text(encoding="utf-8")
    assert "DEVELOPING" in text
    assert "无新 commit" in text


def test_writer_noise_event_writes_nothing(tmp_path):
    p = tmp_path / "session.log"
    w = SessionLogWriter(p)
    w.write_event({"type": "stream_event", "event": {}})
    assert (not p.exists()) or p.read_text(encoding="utf-8") == ""


def test_writer_never_raises_on_bad_path(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    w = SessionLogWriter(d)  # 把目录当日志路径 → 打开必失败，但不能抛
    w.write_note("x")
    w.write_event(_assistant({"type": "text", "text": "y"}))
