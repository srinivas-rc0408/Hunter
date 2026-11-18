"""Optional LLM, through any OpenAI-compatible endpoint. Nothing needs it.

Default LLM_PROVIDER=auto: your local Ollama if it's running, else Gemini's free tier if
GEMINI_API_KEY is set, else Groq's free tier if GROQ_API_KEY is set, else none (templates).
So nothing breaks when a Claude subscription, an API key or Ollama goes away.
Pin one with: LLM_PROVIDER=ollama  LLM_MODEL=qwen2.5:7b-instruct
Free cloud tiers also work:
  LLM_PROVIDER=gemini  GEMINI_API_KEY=...  (aistudio.google.com, free tier)
  LLM_PROVIDER=groq    GROQ_API_KEY=...    (console.groq.com, free tier)
Model IDs get retired (Gemini 2.0 Flash: Jun 2026; Groq Llama 3.3 70B: Aug 2026), so the
model is checked against the provider's live /models list and swapped for the current
equivalent automatically. Override with OLLAMA_MODEL / GEMINI_MODEL / GROQ_MODEL.
  LLM_PROVIDER=openai  LLM_API_KEY=...  LLM_BASE_URL=... for anything else
Without a reachable LLM, cover letters fall back to a factual template and
unknown form questions wait for you.
"""
from __future__ import annotations

import re

import requests

from .config import env

PRESETS = {
    "ollama": ("http://localhost:11434/v1", "qwen2.5:7b-instruct"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.6-flash"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}


KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}
_PICKED: list = []


MODEL_ENV = {"ollama": "OLLAMA_MODEL", "gemini": "GEMINI_MODEL", "groq": "GROQ_MODEL", "openai": "LLM_MODEL"}
PREFER = {   # used when the configured model is not in the provider's live list
    "ollama": [r"^qwen", r"^llama", r"^gemma", r"^mistral", r"."],
    "gemini": [r"^gemini-\d+(\.\d+)?-flash$", r"^gemini-\d+(\.\d+)?-flash-lite$", r"flash"],
    "groq": [r"^openai/gpt-oss-120b$", r"^qwen/qwen3", r"^openai/gpt-oss-20b$", r"."],
    "openai": [r"."],
}


def _models(base: str, key: str | None) -> list[str] | None:
    try:
        r = requests.get(base.rstrip("/") + "/models", timeout=6,
                         headers={"Authorization": f"Bearer {key}"} if key else {})
        if r.status_code >= 400:
            return None
        return [str(m.get("id", "")).removeprefix("models/") for m in r.json().get("data", [])]
    except (requests.RequestException, ValueError, AttributeError):
        return None


def _reachable(base: str, key: str | None) -> bool:
    return _models(base, key) is not None


def _version(mid: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", mid)[:3])


def pick_model(prov: str, wanted: str, ids: list[str]) -> str:
    """`wanted` if the provider still serves it, else the best current equivalent."""
    if not ids or wanted in ids:
        return wanted
    for pat in PREFER.get(prov, [r"."]):
        hits = [m for m in ids if re.search(pat, m) and "preview" not in m and "exp" not in m
                and "embed" not in m and "tts" not in m and "audio" not in m and "image" not in m]
        if hits:
            return sorted(hits, key=_version, reverse=True)[0] if prov == "gemini" else hits[0]
    return wanted


def _resolve(prov: str) -> tuple[str, str, str | None]:
    base, model = PRESETS.get(prov, PRESETS["ollama"])
    pinned = (env("LLM_PROVIDER", "auto") or "auto").lower() == prov
    key = None if prov == "ollama" else (env("LLM_API_KEY") if pinned and env("LLM_API_KEY") else env(KEY_ENV.get(prov, "")))
    base = env("LLM_BASE_URL", base) if pinned else base
    model = env(MODEL_ENV.get(prov, ""), None) or (env("LLM_MODEL", None) if pinned else None) or model
    ids = _models(base, key)
    return base, pick_model(prov, model, ids or []), key


def settings() -> tuple[str, str, str | None]:
    """(base_url, model, api_key) of the provider in use."""
    prov = (env("LLM_PROVIDER", "auto") or "auto").lower()
    if prov != "auto":
        if not _PICKED:
            _PICKED.append(_resolve(prov))
        return _PICKED[0]
    if not _PICKED:
        choice = None
        for p in ("ollama", "gemini", "groq"):
            if p != "ollama" and not env(KEY_ENV[p]):
                continue
            b, m, k = _resolve(p)
            if _models(b, k) is not None:
                choice = (b, m, k)
                break
        _PICKED.append(choice)
    return _PICKED[0] or ("", "none", None)


def available() -> bool:
    if (env("LLM_PROVIDER", "auto") or "").lower() == "none":
        return False
    base, _, key = settings()
    return bool(base) and _reachable(base, key)


def chat(system: str, user: str, temperature: float = 0.3, max_tokens: int = 900) -> str | None:
    base, model, key = settings()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = requests.post(base.rstrip("/") + "/chat/completions", headers=headers, timeout=180, json={
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        if not r.ok:
            return None
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None
