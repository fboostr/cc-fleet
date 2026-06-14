"""解析 claude plan 阶段输出的 SLUG / STATUS / QUESTIONS 协议。

claude 在 plan 末尾按下列格式输出（由 plan_protocol.md 强制）：

    SLUG: <kebab-case-3-to-6-words>
    STATUS: READY
        或
    STATUS: NEED_CLARIFICATION
    QUESTIONS:
    1. 问题 1
    2. 问题 2

主控逐行扫描 text_output 抽取这三个字段。QUESTIONS 段以有序编号呈现（便于用户按编号回复），
解析器同时向后兼容历史 `- ` / `*` / `•` 无序 bullet 格式，避免老 plan.md 与复活 session 失效。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

PlanStatus = Literal["READY", "NEED_CLARIFICATION"]

_SLUG_LINE = re.compile(r"^\s*SLUG:\s*([a-z0-9][a-z0-9-]{2,80})\s*$", re.IGNORECASE | re.MULTILINE)
_STATUS_LINE = re.compile(r"^\s*STATUS:\s*(READY|NEED_CLARIFICATION)\s*$", re.IGNORECASE | re.MULTILINE)
_SLUG_VALID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
# 同时识别有序编号（`1.` / `2)` / `3、`）和向后兼容的无序 bullet（`-` / `*` / `•`）
_QUESTION_BULLET = re.compile(r"^(?:\d+[.)、]|[-*•])\s+(.+)$")


@dataclass
class PlanProtocol:
    slug: str | None
    status: PlanStatus | None
    questions: list[str]


def parse_plan_output(text: str) -> PlanProtocol:
    """从 plan 阶段文本中抽取协议三元组。

    - slug 与 status 各自取**最后一次**出现的命中（避免被前文示例污染）
    - questions 仅在 status==NEED_CLARIFICATION 时解析；从 QUESTIONS: 之后逐行抽取以有序编号
      （`1.` / `2)` / `3、`）开头的条目；同时兼容历史 `-` / `*` / `•` 无序 bullet
    """
    slug_matches = list(_SLUG_LINE.finditer(text))
    status_matches = list(_STATUS_LINE.finditer(text))

    slug = slug_matches[-1].group(1).lower() if slug_matches else None
    status: PlanStatus | None = None
    if status_matches:
        status = status_matches[-1].group(1).upper()  # type: ignore[assignment]

    questions: list[str] = []
    if status == "NEED_CLARIFICATION":
        # 从 QUESTIONS: 标签后逐行抓 bullet
        m = re.search(r"^\s*QUESTIONS:\s*$", text, re.IGNORECASE | re.MULTILINE)
        if m:
            tail = text[m.end():].splitlines()
            for line in tail:
                stripped = line.strip()
                if not stripped:
                    if questions:
                        break
                    continue
                bullet = _QUESTION_BULLET.match(stripped)
                if bullet:
                    questions.append(bullet.group(1).strip())
                else:
                    # 遇到非 bullet 的非空行，QUESTIONS 段落结束
                    break

    return PlanProtocol(slug=slug, status=status, questions=questions)


def strip_plan_protocol_tail(text: str) -> str:
    """剥掉 plan 输出末尾的 SLUG/STATUS/QUESTIONS 协议块，只保留 plan 正文。

    用于把 plan 正文落盘成 ``plan.md`` 供用户查阅。规则：
    - 定位**最后一次**出现的 ``SLUG:`` 行（前文示例不会误伤）
    - 从该行起始位置整段切除（含其后的 STATUS / QUESTIONS bullet）
    - 切除点之前的空白行一并 ``rstrip()``
    - 若文本中找不到任何 ``SLUG:`` 行，返回原文的 ``rstrip()``，调用方仍可正常落盘
    """
    matches = list(_SLUG_LINE.finditer(text))
    if not matches:
        return text.rstrip()
    cut = matches[-1].start()
    return text[:cut].rstrip()


def is_valid_slug(slug: str) -> bool:
    return bool(_SLUG_VALID.match(slug))


async def resolve_slug_conflict(
    base: str,
    exists: Callable[[str], Awaitable[bool]],
    max_attempts: int = 50,
) -> str:
    """若 base 已被占用，依次尝试 base-2, base-3, ... 直到找到空位。

    `exists` 是异步谓词，便于直接接 SQLite 查询。
    """
    if not is_valid_slug(base):
        raise ValueError(f"非法 slug：{base!r}")
    if not await exists(base):
        return base
    for i in range(2, max_attempts + 2):
        candidate = f"{base}-{i}"
        if not await exists(candidate):
            return candidate
    raise RuntimeError(f"slug {base} 冲突解决失败，超出 {max_attempts} 次尝试")
