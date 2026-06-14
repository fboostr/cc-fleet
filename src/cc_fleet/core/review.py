"""解析独立 Reviewer 审查输出的 REVIEW_VERDICT 协议。

Reviewer（plan 审查 / code 审查）在回复末尾按下列格式输出（由 plan_review_protocol.md /
code_review_protocol.md 强制）：

    REVIEW_VERDICT: APPROVED
        或
    REVIEW_VERDICT: NEEDS_REVISION

主控逐行扫描 text_output 抽取该字段：
- APPROVED：plan / 代码可以放行，进入下一阶段。
- NEEDS_REVISION：把审查正文作为反馈交回 Coder 完善。
- 解析不到（status=None）：按「Reviewer 失败即跳过」处理——当作没有 Reviewer 一样继续推进。

与 core/slug.py 同构：取**最后一次**命中（避免被前文示例污染），并提供剥协议尾的工具，
便于把审查正文落盘成 plan_review.md / code_review.md 供查阅。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ReviewStatus = Literal["APPROVED", "NEEDS_REVISION"]

_VERDICT_LINE = re.compile(
    r"^\s*REVIEW_VERDICT:\s*(APPROVED|NEEDS_REVISION)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class ReviewVerdict:
    status: ReviewStatus | None
    body: str  # 剥掉协议尾的审查正文（落盘 + 作为修订反馈用）


def parse_review_output(text: str) -> ReviewVerdict:
    """从 Reviewer 文本中抽取审查结论。

    - status 取**最后一次**出现的 ``REVIEW_VERDICT:`` 命中（避免被前文示例污染）；
      解析不到则为 None，上层按「失败即跳过」处理。
    - body 为剥掉协议尾后的审查正文。
    """
    matches = list(_VERDICT_LINE.finditer(text))
    status: ReviewStatus | None = None
    if matches:
        status = matches[-1].group(1).upper()  # type: ignore[assignment]
    return ReviewVerdict(status=status, body=strip_review_protocol_tail(text))


def strip_review_protocol_tail(text: str) -> str:
    """剥掉审查输出末尾的 ``REVIEW_VERDICT:`` 协议行，只保留审查正文。

    规则与 slug.strip_plan_protocol_tail 一致：定位**最后一次** ``REVIEW_VERDICT:`` 行，
    从该行起整段切除并 ``rstrip()``；找不到则返回原文 ``rstrip()``。
    """
    matches = list(_VERDICT_LINE.finditer(text))
    if not matches:
        return text.rstrip()
    cut = matches[-1].start()
    return text[:cut].rstrip()
