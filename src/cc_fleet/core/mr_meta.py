"""解析 claude dev 阶段输出的 MR_TITLE / MR_DESCRIPTION 协议块。

claude 在 dev 末尾按下列格式输出（由 dev_protocol_local/remote.md 强制）：

    MR_TITLE: <一行中文标题>

    MR_DESCRIPTION_BEGIN
    ## 背景
    ...
    MR_DESCRIPTION_END

主控抽这两段作为 MR 标题与描述；缺失时调用方走 git log 兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MR_TITLE_LINE = re.compile(r"^\s*MR_TITLE:\s*(.+?)\s*$", re.MULTILINE)
# DOTALL 让 .*? 能跨行；MULTILINE 让 ^/$ 在行边界匹配；非贪婪取到最近一个 END
_MR_DESCRIPTION_BLOCK = re.compile(
    r"^\s*MR_DESCRIPTION_BEGIN\s*$\n(.*?)\n^\s*MR_DESCRIPTION_END\s*$",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class MrMetadata:
    title: str | None
    description: str | None


def parse_mr_metadata(text: str) -> MrMetadata:
    """从 dev 阶段文本中抽 MR_TITLE 与 MR_DESCRIPTION 块。

    - MR_TITLE 与 MR_DESCRIPTION 各自取**最后一次**命中（避免被前文示例污染，
      与 ``parse_plan_output`` 同风格）
    - title 自动 ``strip()``；description 保留块内原始换行（仅去掉首尾整段空白）
    - 任一字段缺失返回 ``None``，由调用方走兜底
    """
    if not text:
        return MrMetadata(title=None, description=None)

    title_matches = list(_MR_TITLE_LINE.finditer(text))
    title = title_matches[-1].group(1).strip() if title_matches else None
    if title == "":
        title = None

    desc_matches = list(_MR_DESCRIPTION_BLOCK.finditer(text))
    description = desc_matches[-1].group(1).strip() if desc_matches else None
    if description == "":
        description = None

    return MrMetadata(title=title, description=description)
