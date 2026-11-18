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


def load_env() -> None:
    """Minimal .env loader (KEY=VALUE, # comments). Real env vars win."""
    f = path(".env")
    if not f.is_file():
        return
    for raw in f.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


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


def save_yaml_value(name: str, dotted: str, value: Any) -> None:
    """Set one key in a config YAML, preserving comments where possible.

    Uses a line-level edit for top-level/nested scalar keys that already exist
    (keeps your comments); falls back to a full dump otherwise.
    """
    f = path("config", name if name.endswith(".yaml") else name + ".yaml")
    text = f.read_text(encoding="utf-8") if f.is_file() else ""
    keys = dotted.split(".")
    lines = text.splitlines()
    indent, start = 0, 0
    found = True
    for depth, key in enumerate(keys):
        hit = None
        for i in range(start, len(lines)):
            ln = lines[i]
            stripped = ln.lstrip()
            ind = len(ln) - len(stripped)
            if i > start and depth > 0 and stripped and ind < indent:
                break
            if ind == indent and stripped.startswith(key + ":"):
                hit = i
                break
        if hit is None:
            found = False
            break
        start, indent = hit + 1, indent + 2
        last = hit
    dumped = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True).strip()
    if dumped.endswith("..."):
        dumped = dumped[:-3].strip()
    if found and not isinstance(value, (dict, list)):
        ln = lines[last]
        pre = ln[: ln.index(":") + 1]
        comment = ""
        if "#" in ln[ln.index(":"):]:
            comment = "   " + ln[ln.index("#", ln.index(":")):]
        lines[last] = f"{pre} {dumped}{comment}"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        data = yaml.safe_load(text) or {}
        cur = data
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = value
        f.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    reload()
