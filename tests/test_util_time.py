"""时间工具单元测试。

固定 TZ=Asia/Shanghai 避免 CI 环境差异。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import pytest

from cc_fleet.util.logging import _LocalTZFormatter
from cc_fleet.util.time import now_local_compact, now_local_iso


@pytest.fixture(autouse=True)
def _fixed_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()


def test_now_local_iso_has_local_offset():
    s = now_local_iso()
    # 形如 2026-05-13T11:14:25.123456+08:00
    m = re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2})$", s)
    assert m is not None, f"unexpected format: {s}"
    assert m.group(2) == "+08:00"


def test_now_local_compact_format():
    s = now_local_compact()
    assert re.match(r"^\d{8}-\d{6}$", s), f"unexpected format: {s}"
    assert len(s) == 15


def test_log_formatter_appends_local_tz_offset():
    fmt = _LocalTZFormatter("%(asctime)s %(message)s")
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=None,
        exc_info=None,
    )
    # 强制一个已知时间戳：UTC 2026-05-13 03:14:25 ↔ 本地 +08:00 11:14:25
    record.created = datetime(2026, 5, 13, 3, 14, 25, tzinfo=timezone.utc).timestamp()
    record.msecs = 123.0
    out = fmt.format(record)
    assert out.endswith("hello")
    assert " +0800" in out
    assert "2026-05-13 11:14:25,123" in out
