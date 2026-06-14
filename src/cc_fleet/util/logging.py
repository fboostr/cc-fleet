"""统一日志配置：控制台 + 文件 RotatingFileHandler。"""

from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class _LocalTZFormatter(logging.Formatter):
    """让 %(asctime)s 输出本地时间并附带 `+0800` 形式的时区偏移。

    默认 logging.Formatter 用 time.localtime 生成本地时间但不带时区后缀，
    换机器或排查跨时区问题时不便区分；这里固定输出形如
    `2026-05-13 11:14:25,123 +0800`。
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        ct = datetime.fromtimestamp(record.created).astimezone()
        if datefmt:
            return ct.strftime(datefmt)
        base = ct.strftime("%Y-%m-%d %H:%M:%S")
        return f"{base},{int(record.msecs):03d} {ct.strftime('%z')}"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """配置根 logger：stdout 简洁输出 + 文件 10MB×5 滚动。

    可重入：重复调用会先清空已有 handler。
    """
    log_dir = log_dir.expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = _LocalTZFormatter(_LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def session_logger(log_dir: Path, slug: str) -> logging.Logger:
    """为单个 session 单独建一个 logger，写到 sessions/<slug>.log，不污染主日志。"""
    log_dir = log_dir.expanduser()
    (log_dir / "sessions").mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"session.{slug}")
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_dir / "sessions" / f"{slug}.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(_LocalTZFormatter(_LOG_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
