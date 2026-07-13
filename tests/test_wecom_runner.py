"""WecomBotRunner：shutdown / 消息分发 / 节流 / 主动发消息。"""

from __future__ import annotations

import asyncio
import time

import pytest

from cc_fleet.bot.wecom import runner as runner_mod
from cc_fleet.bot.wecom.runner import WecomBotRunner, _extract_quote_text
from cc_fleet.bot.base import BotDeliveryError


async def _noop(_m) -> None:
    return None


def _runner(allowed=None, on_message=_noop) -> WecomBotRunner:
    return WecomBotRunner(
        bot_id="x", bot_secret="y", allowed_chatids=allowed, on_message=on_message
    )


class _FakeWS:
    """disconnect 同步返回 None，模拟 aibot 的真实签名。"""

    def __init__(self) -> None:
        self.disconnected = 0

    def disconnect(self) -> None:
        self.disconnected += 1


async def test_shutdown_calls_sync_disconnect_without_await():
    runner = _runner()
    fake = _FakeWS()
    runner._ws = fake  # type: ignore[assignment]
    await runner.shutdown()  # 不应抛 'NoneType' object can't be awaited
    assert fake.disconnected == 1


async def test_shutdown_swallows_disconnect_error(caplog):
    runner = _runner()

    class _Boom:
        def disconnect(self) -> None:
            raise RuntimeError("boom")

    runner._ws = _Boom()  # type: ignore[assignment]
    with caplog.at_level("ERROR"):
        await runner.shutdown()  # 异常被吞，不向上抛
    assert any("断开失败" in r.message for r in caplog.records)


# ---------- _extract_quote_text ----------

def test_extract_quote_text_plain():
    assert _extract_quote_text({"msgtype": "text", "text": {"content": " q "}}) == "q"


def test_extract_quote_text_mixed_joins_text_items():
    q = {
        "msgtype": "mixed",
        "mixed": {
            "items": [
                {"type": "text", "text": {"content": "a"}},
                {"type": "image"},
                {"type": "text", "text": {"content": "b"}},
            ]
        },
    }
    assert _extract_quote_text(q) == "a b"


def test_extract_quote_text_empty_or_unknown():
    assert _extract_quote_text({}) == ""
    assert _extract_quote_text({"msgtype": "image"}) == ""


# ---------- _dispatch ----------

async def test_dispatch_builds_message_and_invokes_on_message():
    received = []

    async def on_msg(m):
        received.append(m)

    runner = _runner(on_message=on_msg)
    frame = {
        "body": {
            "chatid": "room1",
            "from": {"userid": "u9"},
            "text": {"content": "  hello  "},
            "quote": {"msgtype": "text", "text": {"content": "ctx"}},
        }
    }
    await runner._dispatch(frame)
    assert len(received) == 1
    m = received[0]
    assert m.text == "hello"  # 去除首尾空白
    assert m.chatid == "room1"
    assert m.userid == "u9"
    assert m.quote_text == "ctx"


async def test_dispatch_filters_non_whitelisted_chatid():
    received = []

    async def on_msg(m):
        received.append(m)

    runner = _runner(allowed=["allowed"], on_message=on_msg)
    await runner._dispatch({"body": {"chatid": "other", "text": {"content": "hi"}}})
    assert received == []


async def test_dispatch_skips_empty_text():
    received = []

    async def on_msg(m):
        received.append(m)

    runner = _runner(on_message=on_msg)
    await runner._dispatch({"body": {"chatid": "c", "text": {"content": "   "}}})
    assert received == []


# ---------- reply ----------

async def test_reply_rejects_empty_chatid():
    runner = _runner()
    sent = []

    class _WS:
        async def send_message(self, *a, **k):
            sent.append(a)

    runner._ws = _WS()  # type: ignore[assignment]
    with pytest.raises(BotDeliveryError, match="缺少 chatid"):
        await runner.reply("", "hi")
    assert sent == []


async def test_reply_sends_markdown_payload():
    runner = _runner()
    runner._last_send_time = time.monotonic() - 100  # 跳过最小发送间隔
    sent = []

    class _WS:
        async def send_message(self, chatid, payload):
            sent.append((chatid, payload))

    runner._ws = _WS()  # type: ignore[assignment]
    await runner.reply("room", "**hi**")
    assert len(sent) == 1
    chatid, payload = sent[0]
    assert chatid == "room"
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["content"] == "**hi**"


async def test_reply_retries_then_raises_send_error(caplog, monkeypatch):
    runner = _runner()
    runner._last_send_time = time.monotonic() - 100

    class _WS:
        calls = 0

        async def send_message(self, *a, **k):
            self.calls += 1
            raise RuntimeError("net down")

    ws = _WS()
    runner._ws = ws  # type: ignore[assignment]

    async def no_sleep(_secs):
        return None

    monkeypatch.setattr(runner_mod.asyncio, "sleep", no_sleep)
    with caplog.at_level("WARNING"), pytest.raises(BotDeliveryError, match="重试 3 次"):
        await runner.reply("room", "x")
    assert ws.calls == 3
    assert any("发送企微消息失败" in r.message for r in caplog.records)


# ---------- _acquire_send_slot 滑动窗口节流 ----------

async def test_acquire_send_slot_within_limit_records_timestamp():
    runner = _runner()
    await runner._acquire_send_slot("c1")
    assert len(runner._send_timestamps["c1"]) == 1


async def test_acquire_send_slot_prunes_stale_timestamps():
    runner = _runner()
    # 预置一批 120s 前的旧时间戳：超出 60s 窗口，应被剔除而不计入限额
    runner._send_timestamps["c1"] = [time.monotonic() - 120] * runner_mod._RATE_LIMIT_PER_MIN
    await runner._acquire_send_slot("c1")  # 不应阻塞
    assert len(runner._send_timestamps["c1"]) == 1  # 旧的清掉，仅剩刚加的 1 条


async def test_acquire_send_slot_waits_when_window_full(monkeypatch):
    runner = _runner()
    # 窗口内塞满限额条最新时间戳 → 应进入等待分支
    runner._send_timestamps["c1"] = [time.monotonic()] * runner_mod._RATE_LIMIT_PER_MIN

    class _Slept(Exception):
        pass

    async def fake_sleep(secs):
        raise _Slept(secs)

    monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Slept):
        await runner._acquire_send_slot("c1")
