"""WechatBotRunner 与 ilink 客户端的单元测试。

外部 HTTP 一律不真发：客户端层 monkeypatch ``_post``/``_get``，runner 层注入 FakeClient。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from cc_fleet.bot.wechat.ilink_client import (
    IlinkClient,
    IlinkError,
    _is_ok_ret,
    _parse_message,
)
from cc_fleet.bot.wechat.runner import WechatBotRunner


# ── 测试替身 ────────────────────────────────────────────────


class FakeClient:
    """替代 IlinkClient：记录发送，长轮询默认返回空。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.typing: list[tuple[str, str]] = []
        self.config = {"typing_ticket": "tk"}

    async def get_config(self) -> dict:
        return self.config

    async def send_typing(self, *, to_user_id: str, typing_ticket: str) -> dict:
        self.typing.append((to_user_id, typing_ticket))
        return {"ret": 0}

    async def send_message(self, *, to_user_id: str, context_token: str, text: str) -> dict:
        self.sent.append((to_user_id, context_token, text))
        return {"ret": 0}

    async def get_updates(self, buf: str):  # noqa: ANN201
        return [], buf

    async def close(self) -> None:
        pass


def _runner(tmp_path: Path, *, on_message=None, allowed=None) -> WechatBotRunner:
    async def _noop(_msg) -> None:
        return None

    return WechatBotRunner(
        bot_token="t",
        base_url="http://example.invalid",
        allowed_user_ids=allowed,
        on_message=on_message or _noop,
        cursor_path=tmp_path / "cur.txt",
        refs_path=tmp_path / "refs.jsonl",
    )


def _raw(from_user: str, ctx: str, *texts: str) -> dict:
    return {
        "from_user_id": from_user,
        "to_user_id": "bot@im.bot",
        "context_token": ctx,
        "item_list": [{"type": 1, "text_item": {"text": t}} for t in texts],
    }


def _raw_new_quote(from_user: str, ctx: str, text: str, ref_create_ms: int) -> dict:
    """新版 ilink 引用报文（照抄真机结构）：ref_msg.message_item 只有 msg_id +
    create_time_ms，不含被引用文本。用于验证「时间戳关联」还原。"""
    return {
        "from_user_id": from_user,
        "to_user_id": "bot@im.bot",
        "context_token": ctx,
        "item_list": [
            {
                "type": 1,
                "text_item": {"text": text},
                "ref_msg": {
                    "message_item": {
                        "type": 0,
                        "create_time_ms": ref_create_ms,
                        "is_completed": True,
                        "msg_id": "1234567890123456789",
                        "button_item_list": [],
                    }
                },
            }
        ],
    }


# ── ilink 消息解析 ─────────────────────────────────────────


def test_parse_message_concatenates_text_ignores_media():
    raw = {
        "from_user_id": "a@im.wechat",
        "to_user_id": "b@im.bot",
        "context_token": "ctx",
        "item_list": [
            {"type": 1, "text_item": {"text": "he"}},
            {"type": 2, "image_item": {}},  # 非文本，应忽略
            {"type": 1, "text_item": {"text": "llo"}},
        ],
    }
    m = _parse_message(raw)
    assert m.from_user_id == "a@im.wechat"
    assert m.context_token == "ctx"
    assert m.text == "hello"


def test_parse_message_empty_item_list():
    m = _parse_message({"from_user_id": "a", "item_list": []})
    assert m.text == ""
    assert m.from_user_id == "a"
    assert m.quote_text == ""


def test_parse_message_extracts_quote_from_ref_msg():
    # ilink 引用结构：item.ref_msg.message_item.text_item.text（实测报文）
    raw = {
        "from_user_id": "u@im.wechat",
        "context_token": "ctx",
        "item_list": [
            {
                "type": 1,
                "text_item": {"text": "/plan"},
                "ref_msg": {
                    "message_item": {
                        "type": 1,
                        "text_item": {
                            "text": "plan 已就绪\n[session: sync-readme-bot-tree @cc-fleet sid: e3cd]"
                        },
                    }
                },
            }
        ],
    }
    m = _parse_message(raw)
    assert m.text == "/plan"
    assert "[session: sync-readme-bot-tree" in m.quote_text


def test_parse_message_no_ref_msg_quote_empty():
    m = _parse_message(_raw("u", "ctx", "hi"))
    assert m.quote_text == ""


def test_parse_message_extracts_quote_alt_container_and_leaf():
    # 健壮性：ref 容器换个 key 名（refer_msg）、被引用文本换个叶子 key（content）也应提取出来，
    # 避免像历史回归那样「换一层嵌套/字段名就静默取空」。
    raw = {
        "from_user_id": "u@im.wechat",
        "context_token": "ctx",
        "item_list": [
            {
                "type": 1,
                "text_item": {"text": "/cancel"},
                "refer_msg": {
                    "message_item": {"content": "已取消 [session: alt-shape @r sid: z]"}
                },
            }
        ],
    }
    m = _parse_message(raw)
    assert m.text == "/cancel"
    assert "[session: alt-shape" in m.quote_text


def test_parse_message_extracts_quote_deeper_and_dedupes():
    # 更深/重复嵌套：只要落在 ref 容器内的文本叶子都能收上来，且重复内容去重。
    raw = {
        "from_user_id": "u",
        "item_list": [
            {
                "type": 1,
                "text_item": {"text": "hi"},
                "quoted": {
                    "a": {"b": {"text": "[session: deep @r sid: q]"}},
                    "c": {"text": "[session: deep @r sid: q]"},
                },
            }
        ],
    }
    m = _parse_message(raw)
    assert m.quote_text == "[session: deep @r sid: q]"  # 去重后仅一条


def test_parse_message_non_ref_keys_not_treated_as_quote():
    # 普通文本消息（无 ref/quote/cite 容器）不应误提引用。
    raw = {"from_user_id": "u", "item_list": [{"type": 1, "text_item": {"text": "just text"}}]}
    m = _parse_message(raw)
    assert m.quote_text == ""


def test_parse_message_new_ref_extracts_create_ms_no_text():
    # 新版 ilink 引用：ref 只带 create_time_ms、不含文本 → quote 为空、ref_create_ms 取到。
    m = _parse_message(_raw_new_quote("u@im.wechat", "ctx", "/plan", 1783178653000))
    assert m.text == "/plan"
    assert m.quote_text == ""
    assert m.ref_create_ms == 1783178653000


def test_parse_message_ref_create_ms_none_when_no_ref():
    m = _parse_message(_raw("u", "ctx", "hi"))
    assert m.ref_create_ms is None


# ── ilink 客户端 ───────────────────────────────────────────


def test_client_headers_auth_and_login():
    client = IlinkClient(base_url="http://x", bot_token="tok")
    h = client._headers(auth=True)
    assert h["Authorization"] == "Bearer tok"
    assert h["AuthorizationType"] == "ilink_bot_token"
    assert "X-WECHAT-UIN" in h
    # 登录类调用 auth=False，不需要 token
    assert client._headers(auth=False)["Content-Type"] == "application/json"


def test_client_headers_auth_without_token_raises():
    client = IlinkClient(base_url="http://x", bot_token=None)
    with pytest.raises(RuntimeError):
        client._headers(auth=True)


class _FakeResp:
    """最小化 aiohttp 响应替身，供 _read_json / _post 测试。"""

    def __init__(self, text: str, status: int = 200, content_type: str = "application/octet-stream"):
        self._text = text
        self.status = status
        self.headers = {"Content-Type": content_type}

    async def text(self) -> str:
        return self._text

    def raise_for_status(self) -> None:
        return None


class _FakeCtx:
    def __init__(self, resp: _FakeResp):
        self._resp = resp

    async def __aenter__(self) -> _FakeResp:
        return self._resp

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    """让 IlinkClient._post/_get 不真发 HTTP，直接吐预设响应。"""

    def __init__(self, resp: _FakeResp):
        self._resp = resp

    def post(self, *a, **k) -> _FakeCtx:
        return _FakeCtx(self._resp)

    def get(self, *a, **k) -> _FakeCtx:
        return _FakeCtx(self._resp)


def _client_with_resp(monkeypatch, resp: _FakeResp) -> IlinkClient:
    client = IlinkClient(base_url="http://x", bot_token="tok")

    async def fake_ensure() -> _FakeSession:
        return _FakeSession(resp)

    monkeypatch.setattr(client, "_ensure_session", fake_ensure)
    return client


# ── ilink ret 语义检查 ─────────────────────────────────────


def test_is_ok_ret_variants():
    assert _is_ok_ret({})            # 缺省视为成功
    assert _is_ok_ret({"ret": 0})    # int 0
    assert _is_ok_ret({"ret": "0"})  # 字符串 "0"
    assert not _is_ok_ret({"ret": 1})
    assert not _is_ok_ret({"ret": "1"})


async def test_post_raises_ilink_error_on_nonzero_ret(monkeypatch):
    client = _client_with_resp(monkeypatch, _FakeResp('{"ret": 1, "errmsg": "bad ctx"}'))
    with pytest.raises(IlinkError) as ei:
        await client._post("/ilink/bot/sendmessage", {})
    assert ei.value.ret == 1
    assert "bad ctx" in str(ei.value)


async def test_post_ok_on_zero_ret(monkeypatch):
    client = _client_with_resp(monkeypatch, _FakeResp('{"ret": "0", "x": 7}'))
    data = await client._post("/ilink/bot/sendmessage", {})
    assert data["x"] == 7


async def test_read_json_tolerates_wrong_content_type():
    # ilink 对 get_bot_qrcode 等返回 octet-stream，但 body 是 JSON 文本
    data = await IlinkClient._read_json(_FakeResp('{"qrcode": "abc"}'))
    assert data["qrcode"] == "abc"


async def test_read_json_non_json_raises_with_snippet():
    with pytest.raises(RuntimeError, match="非 JSON"):
        await IlinkClient._read_json(_FakeResp("<html>boom</html>", content_type="text/html"))


async def test_client_send_message_payload(monkeypatch):
    client = IlinkClient(base_url="http://x", bot_token="tok")
    captured: dict = {}

    async def fake_post(path, payload, *, auth=True):
        captured["path"] = path
        captured["payload"] = payload
        return {"ret": 0}

    monkeypatch.setattr(client, "_post", fake_post)
    await client.send_message(to_user_id="u@im.wechat", context_token="ctx", text="hi")

    assert captured["path"].endswith("/ilink/bot/sendmessage")
    payload = captured["payload"]
    msg = payload["msg"]
    assert msg["to_user_id"] == "u@im.wechat"
    assert msg["context_token"] == "ctx"
    assert msg["message_type"] == 2 and msg["message_state"] == 2
    item = msg["item_list"][0]
    assert item["type"] == 1
    assert item["text_item"]["text"] == "hi"
    # 实测必需字段：client_id（每条唯一）、from_user_id、顶层 base_info——缺 client_id 会被静默丢弃
    assert msg["client_id"]
    assert "from_user_id" in msg
    assert payload["base_info"]["channel_version"]


async def test_client_get_updates_parses(monkeypatch):
    client = IlinkClient(base_url="http://x", bot_token="tok")

    async def fake_post(path, payload, *, auth=True):
        assert payload["get_updates_buf"] == ""
        return {
            "ret": 0,
            "get_updates_buf": "buf2",
            "msgs": [_raw("a", "c", "he", "llo")],
        }

    monkeypatch.setattr(client, "_post", fake_post)
    msgs, buf = await client.get_updates("")
    assert buf == "buf2"
    assert len(msgs) == 1
    assert msgs[0].text == "hello"
    assert msgs[0].from_user_id == "a"
    assert msgs[0].context_token == "c"


# ── runner：收消息归一化 ───────────────────────────────────


async def test_handle_delivers_incoming_and_stores_token(tmp_path):
    got = []

    async def on_msg(m):
        got.append(m)

    runner = _runner(tmp_path, on_message=on_msg)
    runner._client = FakeClient()
    await runner._handle(_parse_message(_raw("alice@im.wechat", "ctx1", "hello")))

    assert len(got) == 1
    assert got[0].userid == "alice@im.wechat"
    assert got[0].chatid == ""  # 单聊无群 chatid
    assert got[0].text == "hello"
    assert runner._context_tokens["alice@im.wechat"][0] == "ctx1"


async def test_handle_empty_text_skipped_but_token_stored(tmp_path):
    got = []

    async def on_msg(m):
        got.append(m)

    runner = _runner(tmp_path, on_message=on_msg)
    runner._client = FakeClient()
    # 非文本消息：正文为空，但仍要记下 context_token
    await runner._handle(
        _parse_message(
            {
                "from_user_id": "alice",
                "context_token": "ctxN",
                "item_list": [{"type": 2, "image_item": {}}],
            }
        )
    )
    assert got == []
    assert runner._context_tokens["alice"][0] == "ctxN"


async def test_handle_without_context_token_warns_and_keeps_old(tmp_path, caplog):
    runner = _runner(tmp_path)
    runner._client = FakeClient()
    # 先收到一条带 token 的消息
    await runner._handle(_parse_message(_raw("alice", "old-token", "hi")))
    assert runner._context_tokens["alice"][0] == "old-token"
    # 再收到一条不带 context_token 的消息：应告警且沿用旧 token，不覆盖
    with caplog.at_level("WARNING"):
        await runner._handle(_parse_message(_raw("alice", "", "again")))
    assert runner._context_tokens["alice"][0] == "old-token"
    assert any("不带 context_token" in r.message for r in caplog.records)


async def test_whitelist_filters_unknown_user(tmp_path):
    got = []

    async def on_msg(m):
        got.append(m)

    runner = _runner(tmp_path, on_message=on_msg, allowed=["alice"])
    runner._client = FakeClient()
    await runner._handle(_parse_message(_raw("bob", "c", "hi")))
    assert got == []
    await runner._handle(_parse_message(_raw("alice", "c2", "hi")))
    assert len(got) == 1 and got[0].userid == "alice"


async def test_handle_passes_quote_text(tmp_path):
    got = []

    async def on_msg(m):
        got.append(m)

    runner = _runner(tmp_path, on_message=on_msg)
    runner._client = FakeClient()
    raw = {
        "from_user_id": "alice",
        "context_token": "ctx",
        "item_list": [
            {
                "type": 1,
                "text_item": {"text": "/plan"},
                "ref_msg": {
                    "message_item": {
                        "type": 1,
                        "text_item": {"text": "x [session: foo-bar @r sid: y] z"},
                    }
                },
            }
        ],
    }
    await runner._handle(_parse_message(raw))
    assert len(got) == 1
    assert got[0].text == "/plan"
    assert "[session: foo-bar" in got[0].quote_text


async def test_handle_resolves_quote_by_timestamp(tmp_path):
    # 新版引用（只带 create_time_ms）：先记录一条「发送时刻→标签」，再收到 create_time_ms
    # 落在容差内的引用消息 → quote_text 按时间戳还原成该标签，下游即可反解 slug。
    got = []

    async def on_msg(m):
        got.append(m)

    runner = _runner(tmp_path, on_message=on_msg)
    runner._client = FakeClient()
    now = int(time.time() * 1000)
    tag = "[session: foo-bar @r sid: abcd1234]"
    runner._record_outbound_ref(now, tag)
    # 引用消息的 create_time_ms 与发送时刻差 300ms（容差内）
    await runner._handle(_parse_message(_raw_new_quote("alice", "ctx", "/plan", now + 300)))
    assert len(got) == 1
    assert got[0].text == "/plan"
    assert got[0].quote_text == tag


async def test_handle_ref_by_time_no_match_warns(tmp_path, caplog):
    # 新版引用但没有匹配的已记录会话消息（历史/非会话消息）→ quote 空 + WARNING，不抛异常。
    got = []

    async def on_msg(m):
        got.append(m)

    runner = _runner(tmp_path, on_message=on_msg)
    runner._client = FakeClient()
    with caplog.at_level("WARNING"):
        await runner._handle(_parse_message(_raw_new_quote("alice", "ctx", "/plan", 1783178653000)))
    assert len(got) == 1
    assert got[0].quote_text == ""
    assert any("未匹配到已记录" in r.message for r in caplog.records)


async def test_handle_sends_typing(tmp_path):
    runner = _runner(tmp_path)
    fake = FakeClient()
    runner._client = fake
    await runner._handle(_parse_message(_raw("alice", "ctx", "hi")))
    assert fake.typing == [("alice", "tk")]


# ── runner：reply 用 context_token ─────────────────────────


async def test_reply_uses_stored_context_token(tmp_path):
    runner = _runner(tmp_path)
    fake = FakeClient()
    runner._client = fake
    await runner._handle(_parse_message(_raw("alice", "ctx1", "hi")))
    await runner.reply("alice", "reply-text")
    assert fake.sent == [("alice", "ctx1", "reply-text")]


async def test_reply_without_context_token_is_noop(tmp_path):
    runner = _runner(tmp_path)
    fake = FakeClient()
    runner._client = fake
    await runner.reply("ghost", "x")  # 没收到过该用户消息
    assert fake.sent == []


async def test_reply_empty_chatid_is_noop(tmp_path):
    runner = _runner(tmp_path)
    fake = FakeClient()
    runner._client = fake
    await runner.reply("", "x")
    assert fake.sent == []


async def test_reply_swallows_send_error(tmp_path, caplog):
    """send_message 抛 IlinkError（ret!=0）时，reply 不向上抛、只记 error。"""
    runner = _runner(tmp_path)
    fake = FakeClient()

    async def boom(*, to_user_id, context_token, text):
        raise IlinkError("/ilink/bot/sendmessage", 1, {"ret": 1})

    fake.send_message = boom
    runner._client = fake
    await runner._handle(_parse_message(_raw("alice", "ctx1", "hi")))
    with caplog.at_level("ERROR"):
        await runner.reply("alice", "boom-text")  # 不应抛
    assert any("发送 ilink 消息失败" in r.message for r in caplog.records)


# ── runner：长轮询推进游标 + 持久化 ────────────────────────


async def test_run_forever_dispatches_in_order_and_persists_cursor(tmp_path):
    got = []

    async def on_msg(m):
        got.append(m.text)

    runner = _runner(tmp_path, on_message=on_msg)
    fake = FakeClient()
    # 一批两条，验证 worker 顺序消费
    batch = [_parse_message(_raw("alice", "c", "first")), _parse_message(_raw("alice", "c", "second"))]
    n = {"i": 0}

    async def fake_get_updates(buf):
        n["i"] += 1
        if n["i"] == 1:
            return batch, "buf-after-1"
        runner._stop = True  # 第二轮即停
        return [], buf

    fake.get_updates = fake_get_updates
    runner._client = fake

    await runner.run_forever()
    # run_forever 入队即返回，需等 worker 把队列消费完
    await asyncio.wait_for(runner._inbox.join(), timeout=2)

    assert got == ["first", "second"]  # 保序
    assert runner._cursor == "buf-after-1"
    assert (tmp_path / "cur.txt").read_text(encoding="utf-8").strip() == "buf-after-1"
    await runner.shutdown()  # 收尾：取消 worker


def test_cursor_persist_roundtrip(tmp_path):
    r1 = _runner(tmp_path)
    r1._cursor = "abc123"
    r1._persist_cursor()
    assert (tmp_path / "cur.txt").read_text(encoding="utf-8").strip() == "abc123"
    # 新实例从同一路径加载游标
    r2 = _runner(tmp_path)
    assert r2._cursor == "abc123"


# ── runner：节流（最小发送间隔）────────────────────────────


async def test_reply_respects_min_interval(tmp_path, monkeypatch):
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        await real_sleep(0)

    monkeypatch.setattr("cc_fleet.bot.wechat.runner.asyncio.sleep", fake_sleep)

    runner = _runner(tmp_path)
    fake = FakeClient()
    runner._client = fake
    runner._context_tokens["alice"] = ("ctx", time.monotonic())

    await runner.reply("alice", "a")
    await runner.reply("alice", "b")  # 紧接着发，应触发最小间隔等待

    assert fake.sent == [("alice", "ctx", "a"), ("alice", "ctx", "b")]
    assert any(s > 0 for s in slept)


# ── runner：出站标签记录 + 引用时间戳反查 ──────────────────────


async def test_reply_records_session_tag_only_when_present(tmp_path):
    runner = _runner(tmp_path)
    runner._client = FakeClient()
    runner._context_tokens["alice"] = ("ctx", time.monotonic())
    # 带 [session:] 标签的回复 → 记录一条（标签为原样子串）
    await runner.reply("alice", "plan 已就绪 ✅\n\n[session: foo @r sid: abcd1234]")
    assert len(runner._outbound_refs) == 1
    assert runner._outbound_refs[0][1] == "[session: foo @r sid: abcd1234]"
    # 不带标签的回复 → 不记录
    await runner.reply("alice", "普通提示，无标签")
    assert len(runner._outbound_refs) == 1


def test_resolve_ref_by_time_within_and_outside_tolerance(tmp_path):
    runner = _runner(tmp_path)
    now = int(time.time() * 1000)
    tag = "[session: foo @r sid: x]"
    runner._record_outbound_ref(now, tag)
    assert runner._resolve_ref_by_time(now) == tag           # 精确
    assert runner._resolve_ref_by_time(now + 4000) == tag     # 容差内(4s)
    assert runner._resolve_ref_by_time(now + 10000) == ""     # 超容差(10s)


def test_resolve_ref_by_time_picks_nearest(tmp_path):
    runner = _runner(tmp_path)
    now = int(time.time() * 1000)
    runner._record_outbound_ref(now, "[session: aaa @r sid: 1]")
    runner._record_outbound_ref(now + 2000, "[session: bbb @r sid: 2]")
    assert "bbb" in runner._resolve_ref_by_time(now + 1600)
    assert "aaa" in runner._resolve_ref_by_time(now + 400)


def test_outbound_refs_persist_roundtrip(tmp_path):
    now = int(time.time() * 1000)
    r1 = _runner(tmp_path)
    r1._record_outbound_ref(now, "[session: foo @r sid: x]")
    # 新实例从同一 refs_path 加载后仍能反查
    r2 = _runner(tmp_path)
    assert r2._resolve_ref_by_time(now + 500) == "[session: foo @r sid: x]"
