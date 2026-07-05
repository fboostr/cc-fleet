"""把 claude stream-json 事件渲染成人类可读的 per-session 运行日志。

背景：每个 session 的原始事件已全量落在 ``sessions/<slug>/stream.jsonl``（工具输入、
返回、守卫阻断都在），但它是原始 stream-json、~90% 是流式增量/心跳噪声、单文件可达 MB
级，人无法直接读；且**终态失败判决**（如「dev 阶段结束但 worktree 无新 commit」）是主控
在子进程跑完后下的、根本不在事件流里。本模块产出 ``session.log``：一处可读、去噪、含
「claude 做了啥 + 主控为什么判失败」的完整叙事，出问题打开一个文件即可。

- ``render_event``：纯函数，一条事件 → 可读行（不含时间戳）；噪声/未知事件返回 ``[]``。
- ``SessionLogWriter``：把渲染结果带时间戳追加进 ``session.log``；阶段流转 / 失败 / 通知
  也旁挂进来。所有写入 try/except 包裹——可读日志是辅助产物，写失败只告警，绝不拖垮 session。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_TRUNC_MARK = "\n…[已省略 {n} 字符,完整见 stream.jsonl]…\n"


def _smart_trunc(s: object, max_chars: int) -> str:
    """普通大小原样，超阈值保留首尾并标注省略量、指向 stream.jsonl。"""
    text = s if isinstance(s, str) else str(s)
    if len(text) <= max_chars:
        return text
    keep = max(1, max_chars // 2)
    omitted = len(text) - 2 * keep
    return f"{text[:keep]}{_TRUNC_MARK.format(n=omitted)}{text[-keep:]}"


def _content_blocks(evt: dict) -> list:
    msg = evt.get("message")
    if not isinstance(msg, dict):
        return []
    blocks = msg.get("content")
    return blocks if isinstance(blocks, list) else []


def _result_content_to_str(content: object) -> str:
    """tool_result 的 content 兼容 str 与 [{type:text,text:..}] 两种形态。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or json.dumps(b, ensure_ascii=False))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _summarize_tool_use(name: str | None, inp: object, max_chars: int) -> str:
    """按工具类型抽出最有信息量的输入摘要（Bash→命令、Read/Write→路径…），再截断。

    刻意不整段吐 Write 的 content / 巨型输入——那些体量大且价值低，只记路径与字符数。
    """
    if not isinstance(inp, dict):
        return _smart_trunc(inp, max_chars)
    n = name or "?"
    if n == "Bash":
        s: str = inp.get("command", "")
    elif n in ("Read", "Edit", "MultiEdit"):
        s = f"file_path={inp.get('file_path', '')}"
        if inp.get("offset") or inp.get("limit"):
            s += f" offset={inp.get('offset')} limit={inp.get('limit')}"
    elif n == "Write":
        s = f"file_path={inp.get('file_path', '')} ({len(inp.get('content', ''))} 字符)"
    elif n == "NotebookEdit":
        s = f"notebook_path={inp.get('notebook_path', '')}"
    elif n == "Grep":
        s = f"pattern={inp.get('pattern', '')}"
        if inp.get("path"):
            s += f" path={inp.get('path')}"
    elif n == "Glob":
        s = f"pattern={inp.get('pattern', '')}"
    elif n in ("Task", "Agent"):
        prompt_lines = str(inp.get("prompt", "")).splitlines()
        s = inp.get("description") or (prompt_lines[0] if prompt_lines else "")
    elif n in ("WebFetch", "WebSearch"):
        s = inp.get("url") or inp.get("query", "")
    else:
        for k in ("command", "file_path", "path", "pattern", "query", "url", "description", "prompt"):
            if inp.get(k):
                s = f"{k}={inp[k]}"
                break
        else:
            s = json.dumps(inp, ensure_ascii=False)
    return _smart_trunc(s, max_chars)


def _render_assistant(evt: dict, max_chars: int) -> list[str]:
    out: list[str] = []
    for b in _content_blocks(evt):
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            txt = (b.get("text") or "").strip()
            if txt:
                out.append("🤖 " + _smart_trunc(txt, max_chars))
        elif bt == "tool_use":
            summary = _summarize_tool_use(b.get("name"), b.get("input"), max_chars)
            out.append(f"🔧 {b.get('name') or '?'}  {summary}")
        # thinking / 其它块：噪声，跳过
    return out


def _render_user(evt: dict, max_chars: int) -> list[str]:
    out: list[str] = []
    for b in _content_blocks(evt):
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        flag = "⛔ is_error" if b.get("is_error") else "ok"
        content = _smart_trunc(_result_content_to_str(b.get("content")), max_chars)
        out.append(f"  ↳ {flag}  {content}")
    return out


def _render_result(evt: dict) -> list[str]:
    parts = [f"is_error={evt.get('is_error')}"]
    if evt.get("num_turns") is not None:
        parts.append(f"num_turns={evt.get('num_turns')}")
    dur = evt.get("duration_ms")
    if isinstance(dur, (int, float)):
        parts.append(f"duration={round(dur / 1000)}s")
    return ["🏁 result  " + "  ".join(parts)]


def _render_init(evt: dict) -> list[str]:
    return [f"⚙️ init  model={evt.get('model', '')}  cwd={evt.get('cwd', '')}"]


def render_event(evt: dict, *, max_chars: int = 6000) -> list[str]:
    """一条 claude stream-json 事件 → 可读行（不含时间戳）；噪声/未知事件返回 ``[]``。

    渲染：assistant 文本(🤖) / 工具调用(🔧 名+输入摘要)、tool_result(↳ ok|⛔)、
    result 终态(🏁)、system.init 概要(⚙️)。去噪：stream_event 流式增量、system.status
    心跳、thinking_tokens、hook_*、rate_limit_event、thinking 块一律不落地。
    """
    if not isinstance(evt, dict):
        return []
    t = evt.get("type")
    if t == "assistant":
        return _render_assistant(evt, max_chars)
    if t == "user":
        return _render_user(evt, max_chars)
    if t == "result":
        return _render_result(evt)
    if t == "system" and evt.get("subtype") == "init":
        return _render_init(evt)
    return []


class SessionLogWriter:
    """把渲染行带时间戳追加进 ``session.log``；写失败降级为 warning，绝不抛。"""

    def __init__(self, path: str | Path, *, max_chars: int = 6000) -> None:
        self.path = Path(path)
        self.max_chars = max_chars

    def _emit(self, entries: list[str]) -> None:
        if not entries:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().astimezone().strftime("%m-%d %H:%M:%S")
            prefix = f"[{ts}] "
            pad = " " * len(prefix)
            with self.path.open("a", encoding="utf-8") as f:
                for entry in entries:
                    lines = entry.split("\n")
                    f.write(prefix + lines[0] + "\n")
                    for cont in lines[1:]:
                        f.write(pad + cont + "\n")
        except Exception as e:  # noqa: BLE001 — 可读日志是辅助产物，绝不因它拖垮 session
            logger.warning("写 session.log 失败（忽略）：%s：%s", self.path, e)

    def write_event(self, evt: dict) -> None:
        try:
            entries = render_event(evt, max_chars=self.max_chars)
        except Exception as e:  # noqa: BLE001
            logger.warning("render_event 失败（忽略）：%s", e)
            return
        self._emit(entries)

    def write_phase(self, title: str) -> None:
        self._emit([f"── {title} ──"])

    def write_note(self, text: str) -> None:
        self._emit([text])
