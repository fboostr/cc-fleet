"""长文本分段等文本处理工具（无业务依赖，供 commands / chat 等复用）。"""

from __future__ import annotations

# 单条消息字符上限的通用默认值。企微 markdown 单条上限约 4K，这里以 4000 字符为切片阈值。
# 中文场景一字符 ~3 字节时约 12K 字节，仍可能逼近企微硬上限；实际遇到被拒收可调小到 1300
# （中文约对应 4K 字节）。
DEFAULT_CHAT_CHUNK_LIMIT = 4000


def split_for_chat(text: str, limit: int = DEFAULT_CHAT_CHUNK_LIMIT) -> list[str]:
    """把长文本切成不超过 ``limit`` 字符的多段，尽量在段落/行边界处切。

    策略：
    1. 整体 <= limit：原样返回单元素列表。
    2. 超长：贪心切片，先在 ``\\n\\n`` 段落边界回退，找不到再退到 ``\\n`` 行边界，
       最后退到硬切。每片末尾的空白裁掉、下一片开头同样跳过紧贴的空白。
    """
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        end = pos + limit
        if end >= n:
            chunks.append(text[pos:].rstrip())
            break
        # 优先在段落（\n\n）边界回退
        cut = text.rfind("\n\n", pos, end)
        if cut == -1 or cut <= pos:
            # 退到行边界
            cut = text.rfind("\n", pos, end)
        if cut == -1 or cut <= pos:
            # 没有合适边界，硬切
            cut = end
        chunks.append(text[pos:cut].rstrip())
        # 跳过紧贴的换行/空白，避免下一片开头一堆空行
        pos = cut
        while pos < n and text[pos] in ("\n", " ", "\t"):
            pos += 1
    # 极端情况下可能产生空 chunk（如开头就一堆空白），剔除
    return [c for c in chunks if c]


def split_for_chat_with_tag(
    text: str,
    tag: str,
    limit: int = DEFAULT_CHAT_CHUNK_LIMIT,
    extra_reserve: int = 0,
) -> list[str]:
    """切分 ``text`` 并给**每一段**追加 ``tag``，供分段回发时每段都可被引用反解。

    切分阈值会预留 ``len(tag) + extra_reserve`` 的空间，保证拼接 tag（以及调用方可能
    再前置的分页头，用 ``extra_reserve`` 预留）后单段仍不超过 ``limit``——避免只在最后
    一段带 tag 时"引用前段反解不出 session"，同时不因加 tag 顶破企微单条上限。

    ``tag`` 通常自带前置分隔符（如 ``"\\n\\n[session: ...]"``）；空 ``text`` 会退化为
    单元素 ``[tag]``，调用方应自行避免对空文本调用（改发兜底文案）。
    """
    budget = max(1, limit - len(tag) - extra_reserve)
    return [chunk + tag for chunk in split_for_chat(text, budget)]
