"""Optional LLM, through any OpenAI-compatible endpoint. Nothing needs it.

Default: your local Ollama (free, offline):   LLM_PROVIDER=ollama  LLM_MODEL=qwen2.5:7b-instruct
Free cloud tiers also work:
  LLM_PROVIDER=gemini  LLM_API_KEY=...  (aistudio.google.com, free tier)   LLM_MODEL=gemini-2.0-flash
  LLM_PROVIDER=groq    LLM_API_KEY=...  (console.groq.com, free tier)      LLM_MODEL=llama-3.3-70b-versatile
  LLM_PROVIDER=openai  LLM_API_KEY=...  LLM_BASE_URL=... for anything else
Without a reachable LLM, cover letters fall back to a factual template and
unknown form questions wait for you.
"""
from __future__ import annotations

import requests

from .config import env

PRESETS = {
    "ollama": ("http://localhost:11434/v1", "qwen2.5:7b-instruct"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}


def settings() -> tuple[str, str, str | None]:
    prov = (env("LLM_PROVIDER", "ollama") or "ollama").lower()
    base, model = PRESETS.get(prov, PRESETS["ollama"])
    return env("LLM_BASE_URL", base), env("LLM_MODEL", model), env("LLM_API_KEY")


def available() -> bool:
    if (env("LLM_PROVIDER", "") or "").lower() == "none":
        return False
    base, _, key = settings()
    try:
        r = requests.get(base.rstrip("/") + "/models", timeout=4,
                         headers={"Authorization": f"Bearer {key}"} if key else {})
        return r.status_code < 500
    except requests.RequestException:
        return False


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
