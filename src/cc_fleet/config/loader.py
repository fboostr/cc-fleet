"""读取 YAML 配置并展开 ${env:VAR} 占位符。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .schema import AppConfig, PlatformType

_ENV_PATTERN = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")

# 各平台在配置里对应的段名，恰好等于 PlatformType 的值（wecom / wechat）
_PLATFORM_SECTIONS = {p.value for p in PlatformType}


def _drop_unselected_platform_sections(raw: Any) -> Any:
    """只保留 ``platform`` 选中的那个平台段，丢弃其它平台段后再做 env 展开。

    否则「切到 wechat，却因为未使用的 wecom 段里 ``${env:WECOM_*}`` 没设置而启动失败」
    会成为每个换平台用户的摩擦。未选用的平台段本就被 schema 忽略，这里顺手让它也不参与
    env 展开、不要求其环境变量。``platform`` 值非法时不丢弃，交给 pydantic 报清晰的错。
    """
    if not isinstance(raw, dict):
        return raw
    selected = raw.get("platform", PlatformType.WECOM.value)
    if selected in _PLATFORM_SECTIONS:
        for key in _PLATFORM_SECTIONS:
            if key != selected:
                raw.pop(key, None)
    return raw


def _substitute_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            v = os.environ.get(name)
            if v is None:
                raise ValueError(f"环境变量 {name} 未设置")
            return v
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


def load_config(path: str | Path) -> AppConfig:
    """加载 .env（若存在），再读 config.yaml，环境变量占位符现场展开后用 pydantic 校验。"""
    load_dotenv()
    raw = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    raw = _drop_unselected_platform_sections(raw)
    expanded = _substitute_env(raw)
    return AppConfig.model_validate(expanded)
