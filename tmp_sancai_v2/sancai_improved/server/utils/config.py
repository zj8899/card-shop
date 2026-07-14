"""Unified config loading — single entry point for defaults.yaml."""
import os
import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"
_CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.yaml"
_CONFIG_CACHE: dict | None = None

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _resolve_env(value):
    """递归展开字符串中的 ${VAR} 环境变量引用。非字符串直接返回。"""
    if isinstance(value, str):
        return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def get_config() -> dict:
    """Load config/defaults.yaml with in-memory cache + ${VAR} 环境变量展开."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _CONFIG_CACHE = _resolve_env(raw)
    except Exception:
        _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def load_universe() -> list[dict]:
    """Load stock universe from config."""
    return get_config().get("universe", [])
