"""个人微信 ilink ClawBot 的薄 HTTP 客户端。

ilink（智联）是腾讯 2026 年官方开放的个人号 Bot 协议，端点在 ``ilinkai.weixin.qq.com``，
纯 HTTP/JSON，无需 SDK：

- 扫码登录拿 ``bot_token``：``get_bot_qrcode`` → 轮询 ``get_qrcode_status``
- 长轮询收消息：``getupdates``（服务端最长挂 ~35s 等新消息）
- 发消息：``sendmessage``；打字指示：``getconfig`` 取 ticket + ``sendtyping``

本模块只实现「文本收发 + typing」所需端点，**不处理媒体 / AES**（非文本 item 在
``runner`` 侧忽略）。鉴权头与字段名依据社区抓取的协议 spec，集中放在本文件的常量与
``_parse_message`` 里——首次联调若实际响应不同，改这一处即可。

引用消息：实测新版 ilink 的引用（``ref_msg``）只回传被引用消息的 ``msg_id`` +
``create_time_ms``、不再内联其文本，故 ``_ref_text`` 直接取文本常为空；改由
``_ref_create_ms`` 取出创建时间戳，交 ``runner`` 按「发送时刻→标签」记录反查还原
``[session:]`` 标签（见 WechatBotRunner 的时间戳关联）。
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)


class IlinkError(RuntimeError):
    """ilink 接口语义失败（HTTP 200 但响应体 ret != 0）。

    携带请求 path、ret 与完整响应体，便于定位「HTTP 成功但消息没送达」这类静默失败。
    """

    def __init__(self, path: str, ret: object, body: dict) -> None:
        self.path = path
        self.ret = ret
        self.body = body
        super().__init__(f"ilink {path} 失败：ret={ret!r} body={body}")


def _is_ok_ret(body: dict) -> bool:
    """ret 缺省或为 0 / "0" 视为成功（实测 ret 有 int 0 与字符串 "0" 两种）。"""
    return str(body.get("ret", 0)) == "0"


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

# getupdates/getconfig 请求体里的 base_info.channel_version
CHANNEL_VERSION = "1.0.2"

# item_list[].type：1=text 2=image 3=voice 4=file 5=video（首版只认 text）
ITEM_TYPE_TEXT = 1

# sendmessage 出站消息的 message_type / message_state（按 spec 抓取值，联调时核对）
_OUTBOUND_MESSAGE_TYPE = 2
_OUTBOUND_MESSAGE_STATE = 2

# 长轮询客户端超时要略大于服务端 ~35s 的挂起上限
_POLL_TIMEOUT_SEC = 45.0


def _gen_uin() -> str:
    """X-WECHAT-UIN：base64(str(random uint32))。"""
    return base64.b64encode(str(secrets.randbelow(2**32)).encode()).decode()


@dataclass
class IlinkMessage:
    """从 ilink 收到的一条消息（已抽出文本）。

    - text：拼接后的文本（非文本 item 已忽略，可能为空字符串）
    - context_token：回复该会话必须带回的 token（见 runner 的 _context_tokens）
    - msg_id：本条入站消息的唯一 id（ilink snowflake 形态）。供 runner 做入站去重——
      长轮询游标在整批处理成功后才推进，崩溃 / 出站失败重取整批时据此跳过已处理的消息，
      避免同一需求被重复处理（重复建 session）。缺失时为空串（该条不参与去重）。
    - quote_text：被引用消息文本（旧版 ilink 会内联；新版只给指针，故常为空）
    - ref_create_ms：被引用消息的创建时间戳（ms）。新版 ilink 引用只回传它、不含文本，
      runner 据此按时间戳反查我们发出的 session 标签（见 WechatBotRunner._resolve_ref_by_time）
    """

    from_user_id: str
    to_user_id: str
    context_token: str
    text: str
    msg_id: str = ""
    quote_text: str = ""
    ref_create_ms: int | None = None
    raw: dict = field(default_factory=dict)


# 疑似承载「被引用/回复消息」的容器 key（各家协议命名不一，用子串宽松匹配）。
_REF_CONTAINER_HINTS = ("ref", "quote", "cite")
# 被引用文本可能落在这些「文本叶子」key 下（按常见命名收集）。
_REF_TEXT_LEAF_KEYS = ("text", "content", "digest", "desc", "caption", "title", "msg")


def _collect_leaf_texts(obj: object, keys: tuple[str, ...], out: list[str]) -> None:
    """递归收集 ``obj`` 子树里 key 命中 ``keys`` 的非空字符串叶子。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _collect_leaf_texts(v, keys, out)
            elif isinstance(v, str) and v.strip() and str(k).lower() in keys:
                out.append(v)
    elif isinstance(obj, list):
        for it in obj:
            _collect_leaf_texts(it, keys, out)


def _ref_text(item: dict) -> str:
    """提取被引用消息文本，兼容 ilink ref 结构的嵌套差异（无则空串）。

    早期按固定路径 ``item.ref_msg.message_item.text_item.text`` 取值；该路径依据社区抓取的
    spec、未在真机充分验证，一旦 ilink 调整嵌套/字段名就会静默取空（曾导致引用回归：引用
    消息发 /plan、/cancel 时反解不出 ``[session: <slug>]``）。改为宽松提取：在 item 内找出
    疑似「被引用容器」的子树（key 含 ref/quote/cite），在其下递归收集文本叶子后去重拼接。
    """
    if not isinstance(item, dict):
        return ""
    texts: list[str] = []
    for k, v in item.items():
        if any(h in str(k).lower() for h in _REF_CONTAINER_HINTS):
            _collect_leaf_texts(v, _REF_TEXT_LEAF_KEYS, texts)
    seen: set[str] = set()
    uniq: list[str] = []
    for t in texts:
        s = t.strip()
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return "\n".join(uniq).strip()


def _find_int_leaf(obj: object, keys: tuple[str, ...]) -> int | None:
    """递归查找 ``obj`` 子树里首个 key 命中 ``keys`` 的整数叶子（跳过 bool）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, int) and not isinstance(v, bool) and str(k).lower() in keys:
                return v
            r = _find_int_leaf(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = _find_int_leaf(it, keys)
            if r is not None:
                return r
    return None


def _ref_create_ms(item: dict) -> int | None:
    """被引用消息的创建时间戳（ms）：在 item 内的「被引用容器」子树里找 create_time_ms。

    新版 ilink 引用只回传被引用消息的 msg_id + create_time_ms（不含文本），该时间戳与我们
    发送该消息的时刻对齐，runner 据此反查还原 [session:] 标签。无则 None。
    """
    if not isinstance(item, dict):
        return None
    for k, v in item.items():
        if any(h in str(k).lower() for h in _REF_CONTAINER_HINTS):
            ts = _find_int_leaf(v, ("create_time_ms",))
            if ts is not None:
                return ts
    return None


def _parse_message(m: dict) -> IlinkMessage:
    items = m.get("item_list") or []
    texts = [
        ((it.get("text_item") or {}).get("text", "") or "")
        for it in items
        if it.get("type") == ITEM_TYPE_TEXT
    ]
    # 引用/回复：旧版 ilink 把被引用文本内联在 item 的 ref_msg 里（含 [session:] 标签）；
    # 新版只回传被引用消息的 create_time_ms（下方 ref_create_ms），由 runner 按时间反查还原。
    quote = "\n".join(t for t in (_ref_text(it) for it in items) if t).strip()
    ref_create_ms = next(
        (c for c in (_ref_create_ms(it) for it in items) if c is not None), None
    )
    return IlinkMessage(
        from_user_id=m.get("from_user_id", "") or "",
        to_user_id=m.get("to_user_id", "") or "",
        context_token=m.get("context_token", "") or "",
        text="".join(texts).strip(),
        # 顶层 msg_id 为本条消息的唯一 id（与 ref_msg 里回传的 msg_id 同字段名）；缺失留空串。
        msg_id=str(m.get("msg_id") or ""),
        quote_text=quote,
        ref_create_ms=ref_create_ms,
        raw=m,
    )


class IlinkClient:
    """ilink HTTP 端点的异步薄封装；复用单个 aiohttp 会话。

    登录类端点（get_bot_qrcode / get_qrcode_status）不需要 token，可在未配置
    bot_token 时调用；其余端点需要 token，缺失会抛 RuntimeError。
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        bot_token: str | None = None,
        poll_timeout_sec: float = _POLL_TIMEOUT_SEC,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = bot_token
        self._timeout = aiohttp.ClientTimeout(total=poll_timeout_sec)
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _headers(self, *, auth: bool) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if auth:
            if not self._token:
                raise RuntimeError("该 ilink 调用需要 bot_token，但客户端未配置 token")
            h["AuthorizationType"] = "ilink_bot_token"
            h["Authorization"] = f"Bearer {self._token}"
            h["X-WECHAT-UIN"] = _gen_uin()
        return h

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse) -> dict:
        """按响应体文本解析 JSON，不依赖 Content-Type。

        ilink 端点对部分接口返回 ``application/octet-stream``（实为 JSON 文本），
        用 ``resp.json()`` 会因 mimetype 校验失败；故读 text 再 ``json.loads``，
        非 JSON 时抛出带响应片段的 RuntimeError 便于排查协议差异。
        """
        text = await resp.text()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            ct = resp.headers.get("Content-Type", "?")
            raise RuntimeError(
                f"ilink 返回非 JSON（HTTP {resp.status}, content-type={ct}）：{text[:300]}"
            ) from e

    async def _post(self, path: str, payload: dict, *, auth: bool = True) -> dict:
        sess = await self._ensure_session()
        async with sess.post(
            f"{self._base}{path}", json=payload, headers=self._headers(auth=auth)
        ) as resp:
            resp.raise_for_status()
            data = await self._read_json(resp)
        # ilink 即便 HTTP 200 也可能 ret != 0（如 context_token 失效）——显式抛出，
        # 避免「发送看似成功、实际没送达」的静默失败。登录类 GET 语义不同，不在此校验。
        if not _is_ok_ret(data):
            raise IlinkError(path, data.get("ret"), data)
        return data

    async def _get(self, path: str, params: dict, *, auth: bool = False) -> dict:
        sess = await self._ensure_session()
        async with sess.get(
            f"{self._base}{path}", params=params, headers=self._headers(auth=auth)
        ) as resp:
            resp.raise_for_status()
            return await self._read_json(resp)

    # ---------- 扫码登录 ----------

    async def get_bot_qrcode(self) -> dict:
        """返回 {qrcode, qrcode_img_content}（bot_type=3 表示个人号 Bot）。"""
        return await self._get("/ilink/bot/get_bot_qrcode", {"bot_type": "3"}, auth=False)

    async def get_qrcode_status(self, qrcode: str) -> dict:
        """轮询扫码状态；confirmed 时返回 {status, bot_token, baseurl}。"""
        return await self._get(
            "/ilink/bot/get_qrcode_status", {"qrcode": qrcode}, auth=False
        )

    # ---------- 收消息（长轮询）----------

    async def get_updates(self, buf: str) -> tuple[list[IlinkMessage], str]:
        """长轮询拉取新消息，返回 (消息列表, 新游标)。

        ``buf`` 是上次返回的 ``get_updates_buf`` 游标，首次传空串。
        """
        payload = {
            "get_updates_buf": buf or "",
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        data = await self._post("/ilink/bot/getupdates", payload)
        msgs = [_parse_message(m) for m in (data.get("msgs") or [])]
        new_buf = data.get("get_updates_buf") or buf
        return msgs, new_buf

    # ---------- 发消息 ----------

    async def send_message(
        self, *, to_user_id: str, context_token: str, text: str
    ) -> dict:
        """发一条文本消息；context_token 必须取自对应 inbound 消息。

        实测：缺少 ``client_id`` 时 ilink 会返回 ret=0 但**消息不投递**（静默丢弃）。
        故对齐可用客户端，补齐 ``from_user_id``（空串）、每条唯一的 ``client_id``、
        以及顶层 ``base_info``。
        """
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"cc-fleet-{uuid.uuid4().hex}",
                "message_type": _OUTBOUND_MESSAGE_TYPE,
                "message_state": _OUTBOUND_MESSAGE_STATE,
                "context_token": context_token,
                "item_list": [{"type": ITEM_TYPE_TEXT, "text_item": {"text": text}}],
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        return await self._post("/ilink/bot/sendmessage", payload)

    # ---------- 打字指示 ----------

    async def get_config(self) -> dict:
        """取运行配置，含 typing_ticket。"""
        return await self._post(
            "/ilink/bot/getconfig",
            {"base_info": {"channel_version": CHANNEL_VERSION}},
        )

    async def send_typing(self, *, to_user_id: str, typing_ticket: str) -> dict:
        """给指定用户发「正在输入」状态（best-effort）。"""
        return await self._post(
            "/ilink/bot/sendtyping",
            {"to_user_id": to_user_id, "typing_ticket": typing_ticket},
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
