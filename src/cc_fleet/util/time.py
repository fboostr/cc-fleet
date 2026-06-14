"""统一的本地时区时间工具。

集中化设计：所有写入 DB、生成 slug 的时间戳都通过这里产出，
便于后续切换格式 / 单元测试 / 加时区配置项。
"""

from __future__ import annotations

from datetime import datetime


def now_local_iso() -> str:
    """当前本地时区的 ISO8601 字符串，含偏移，例 `2026-05-13T11:14:25.123+08:00`。"""
    return datetime.now().astimezone().isoformat()


def now_local_compact() -> str:
    """当前本地时区的紧凑格式 `YYYYMMDD-HHMMSS`，用于拼到 slug / 分支名后缀。"""
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
