"""util.text 分段工具单测：重点覆盖 split_for_chat_with_tag。

split_for_chat_with_tag 把长文本切段并给**每一段**追加 tag，且切分阈值要预留 tag
（及 extra_reserve）长度，保证拼接 tag 后单段仍不超过 limit——这是"分段消息每段都带
session tag、避免拼接后溢出企微单条上限"这一修复的核心不变式。
"""

from __future__ import annotations

from cc_fleet.util.text import split_for_chat_with_tag

TAG = "\n\n[session: foo @bar sid: deadbeefcafe0001]"


def test_short_text_single_chunk_carries_tag():
    out = split_for_chat_with_tag("短文本", TAG)
    assert out == ["短文本" + TAG]


def test_long_text_every_chunk_carries_tag():
    body = "\n\n".join("A" + "x" * 3000 for _ in range(3))  # > 4000，会分段
    out = split_for_chat_with_tag(body, TAG)
    assert len(out) >= 2, "超长文本应被拆成多段"
    assert all(c.endswith(TAG) for c in out), "每一段都应带 tag"


def test_split_reserves_tag_length_no_overflow():
    body = "x" * 10000  # 无换行，只能硬切
    limit = 4000
    out = split_for_chat_with_tag(body, TAG, limit)
    assert len(out) >= 2
    # 关键不变式：每段（正文 + tag）都不超过 limit
    assert all(len(c) <= limit for c in out)


def test_extra_reserve_further_shrinks_budget():
    body = "x" * 10000
    limit = 4000
    reserve = 200
    out = split_for_chat_with_tag(body, TAG, limit, extra_reserve=reserve)
    # 额外预留后，每段总长再收紧到 limit - reserve 以内（给分页头等留空间）
    assert all(len(c) <= limit - reserve for c in out)
