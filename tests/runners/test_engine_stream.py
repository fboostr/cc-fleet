"""验证 _read_stream_json 的 on_event 回调机制：每行 JSON 触发一次，与落盘解耦。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cc_fleet.core.runners.engine import _read_stream_json


def _make_reader(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


async def test_on_event_called_for_each_json_line(tmp_path: Path):
    lines = [
        {"type": "system", "subtype": "init", "session_id": "abc"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "result": "done"},
    ]
    payload = ("\n".join(json.dumps(x) for x in lines) + "\n").encode("utf-8")
    reader = _make_reader(payload)
    captured: list[dict] = []

    async def on_event(evt: dict) -> None:
        captured.append(evt)

    log = tmp_path / "stream.jsonl"
    events: list[dict] = []
    text, sid = await _read_stream_json(reader, log, events, on_event)

    assert sid == "abc"
    assert "hi" in text or "done" in text  # 至少抽到 assistant/text 或 result/text
    assert len(captured) == 3
    assert captured[0]["type"] == "system"
    # 文件仍要落盘（保留 stream.jsonl）
    assert log.read_text(encoding="utf-8").count("\n") == 3


async def test_on_event_exception_does_not_abort_stream(tmp_path: Path):
    """回调抛异常应被吞掉，后续事件仍能解析。"""
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "b"}]}},
    ]
    payload = ("\n".join(json.dumps(x) for x in lines) + "\n").encode("utf-8")
    reader = _make_reader(payload)

    calls: list[dict] = []

    async def on_event(evt: dict) -> None:
        calls.append(evt)
        if len(calls) == 1:
            raise RuntimeError("boom")

    text, _ = await _read_stream_json(reader, tmp_path / "s.jsonl", [], on_event)
    assert len(calls) == 2
    assert "ab" == text


async def test_on_event_optional(tmp_path: Path):
    """不传 on_event 时行为不变。"""
    payload = (json.dumps({"type": "result", "result": "x"}) + "\n").encode()
    reader = _make_reader(payload)
    text, _ = await _read_stream_json(reader, tmp_path / "s.jsonl", [])
    assert text == "x"


async def test_non_json_line_does_not_trigger_callback(tmp_path: Path):
    payload = b"not-json\n" + json.dumps({"type": "result", "result": "x"}).encode() + b"\n"
    reader = _make_reader(payload)
    calls: list[dict] = []

    async def on_event(evt: dict) -> None:
        calls.append(evt)

    await _read_stream_json(reader, tmp_path / "s.jsonl", [], on_event)
    assert len(calls) == 1
    assert calls[0]["type"] == "result"


async def test_oversized_single_line_does_not_raise(tmp_path: Path):
    """回归：claude 输出单行 JSON 大小可能远超 asyncio StreamReader 默认 64 KiB 行长。
    历史链路靠 ``async for raw_line`` 走 readline，触发 LimitOverrunError 把整轮 drive 打挂。
    chunk 模式必须能完整读完这条超大行并解析。"""
    big_text = "X" * (256 * 1024)  # 256 KiB > 默认 64 KiB
    evt = {"type": "assistant", "message": {"content": [{"type": "text", "text": big_text}]}}
    payload = (json.dumps(evt) + "\n").encode("utf-8")
    assert len(payload) > 64 * 1024

    # 关键：feed_data 一次性塞超过 limit 的数据；StreamReader 默认 limit 在 readline 才会抛。
    # 这里用 read() 路径不受影响，但通过 feed_data 一次喂入仍能模拟"一行 >64KB"场景。
    reader = _make_reader(payload)
    log = tmp_path / "s.jsonl"
    events: list[dict] = []
    text, _ = await _read_stream_json(reader, log, events)
    assert text == big_text
    assert len(events) == 1
    # 落盘内容也完整
    assert big_text in log.read_text(encoding="utf-8")


async def test_chunk_split_across_lines(tmp_path: Path):
    """单个 chunk 跨多行 / 一行跨多个 chunk 都要正确拼接。"""
    lines = [
        {"type": "system", "subtype": "init", "session_id": "s-1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "b"}]}},
    ]
    payload = ("\n".join(json.dumps(x) for x in lines) + "\n").encode("utf-8")

    # 用一个真 StreamReader，把数据按非对齐的小 chunk 多次 feed
    reader = asyncio.StreamReader()
    # 单字节灌入，故意把每条 JSON 切碎
    for i in range(0, len(payload), 7):
        reader.feed_data(payload[i : i + 7])
    reader.feed_eof()

    text, sid = await _read_stream_json(reader, tmp_path / "s.jsonl", [])
    assert sid == "s-1"
    assert text == "ab"


async def test_last_line_without_trailing_newline(tmp_path: Path):
    """EOF 时 buf 末尾残留的最后一行（claude 偶尔在 SIGTERM 时不补 \\n）也要被处理。"""
    payload = json.dumps({"type": "result", "result": "tail"}).encode("utf-8")  # 故意不带 \n
    reader = _make_reader(payload)
    text, _ = await _read_stream_json(reader, tmp_path / "s.jsonl", [])
    assert text == "tail"
