"""Paths, YAML config and .env loading. Everything resolves from one home dir."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def home() -> Path:
    env = os.environ.get("JOBHUNTER_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def path(*parts: str) -> Path:
    return home().joinpath(*parts)


def data_dir() -> Path:
    d = path("data")
    d.mkdir(parents=True, exist_ok=True)
    return d


_FROM_FILE: dict[str, str] = {}   # what load_env set, so runtime changes are never overwritten


def load_env() -> None:
    """Minimal .env loader (KEY=VALUE, # comments). Real env vars win.
    The first non-empty value of a key wins, so an empty `KEY=` line copied from
    .env.example never hides a real `KEY=value` further down."""
    f = path(".env")
    if not f.is_file():
        return
    vals: dict[str, str] = {}
    for raw in f.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and (k not in vals or (not vals[k] and v)):
            vals[k] = v
    for k, v in vals.items():
        if k not in os.environ or (k in _FROM_FILE and os.environ[k] == _FROM_FILE[k]):
            os.environ[k] = v
            _FROM_FILE[k] = v


def env(key: str, default: str | None = None) -> str | None:
    load_env()
    v = os.environ.get(key)
    return v if v not in (None, "") else default


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    f = path("config", name)
    if not f.is_file():
        return {}
    with f.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=None)
def _profile() -> dict:
    """profile.yaml (public) with private.yaml (never committed) merged on top."""
    return deep_merge(_load("profile.yaml"), _load("private.yaml"))


def cfg(name: str) -> dict:
    """cfg('profile') → config/profile.yaml (+ private.yaml) as dict."""
    if name in ("profile", "profile.yaml"):
        return _profile()
    return _load(name if name.endswith(".yaml") else name + ".yaml")


def reload() -> None:
    _load.cache_clear()
    _profile.cache_clear()


def get(d: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


