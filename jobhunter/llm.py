"""Optional LLM, through any OpenAI-compatible endpoint. Nothing needs it.

LLM_PROVIDER=auto (default) tries, per request, in this order:
  1. NVIDIA build (integrate.api.nvidia.com), if NVIDIA_API_KEYS / NVIDIA_API_KEY is set
  2. your local Ollama, if it's running (default model qwen3:4b, fits a 4 GB GPU)
  3. Gemini free tier (GEMINI_API_KEY)   4. Groq free tier (GROQ_API_KEY)
  5. none: cover letters use a factual template, unknown form questions wait for you.
A failure anywhere (bad key, rate limit, timeout, retired model) moves on to the next
key, then the next model, then the next provider. So nothing breaks when a Claude
subscription, an API key, a free tier or Ollama goes away.

Several keys per provider: NVIDIA_API_KEYS=nvapi-a,nvapi-b,nvapi-c (or NVIDIA_API_KEY,
NVIDIA_API_KEY_2, NVIDIA_API_KEY_3; the same pattern works for GEMINI_ / GROQ_). A key
that gets 401/403 is skipped for the rest of the run; one that gets 429 rests 60 s.

Model IDs get retired (Gemini 2.0 Flash: Jun 2026; Groq Llama 3.3 70B: Aug 2026), so the
model is checked against the provider's live /models list and swapped for the best current
equivalent automatically. Override with NVIDIA_MODEL / OLLAMA_MODEL / GEMINI_MODEL / GROQ_MODEL.
Pin one provider: LLM_PROVIDER=nvidia|ollama|gemini|groq|openai|none (+ LLM_MODEL, LLM_API_KEY,
LLM_BASE_URL for any other OpenAI-compatible endpoint).

Reasoning models' <think>…</think> blocks are stripped from replies.
"""
from __future__ import annotations

import re
import time

import requests

from .config import env

PRESETS = {
    "nvidia": ("https://integrate.api.nvidia.com/v1", "deepseek-ai/deepseek-v4-flash"),
    "ollama": ("http://localhost:11434/v1", "qwen3:4b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.6-flash"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}
CHAIN = ("nvidia", "ollama", "gemini", "groq")
KEY_ENV = {"nvidia": "NVIDIA_API_KEY", "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}
MODEL_ENV = {"nvidia": "NVIDIA_MODEL", "ollama": "OLLAMA_MODEL", "gemini": "GEMINI_MODEL", "groq": "GROQ_MODEL",
             "openai": "LLM_MODEL"}
PREFER = {   # best first; used when the configured model is gone, and for fallback models
    "nvidia": [r"^deepseek-ai/deepseek-v4-flash$", r"^qwen/qwen3-next-80b-a3b-instruct$",
               r"^moonshotai/kimi-k2-instruct$", r"^meta/llama-3\.3-70b-instruct$",
               r"^mistralai/mistral-nemotron$", r"^z-ai/glm-?5", r"^openai/gpt-oss-120b$",
               r"^nvidia/nemotron-3", r"^nvidia/llama-3\.3-nemotron-super", r"instruct",
               r"deepseek|qwen|kimi|glm|llama|mistral|nemotron|gemma|gpt-oss|minimax|step"],
    "ollama": [r"^qwen3", r"^phi4", r"^qwen", r"^llama", r"^gemma", r"^mistral", r"."],
    "gemini": [r"^gemini-\d+(\.\d+)?-flash$", r"^gemini-\d+(\.\d+)?-flash-lite$", r"flash"],
    "groq": [r"^openai/gpt-oss-120b$", r"^qwen/qwen3", r"^openai/gpt-oss-20b$", r"."],
    "openai": [r"."],
}
SKIP = ("preview", "exp", "embed", "tts", "audio", "image", "vision", "guard", "safety", "reward",
        "rerank", "translate", "pii", "jailbreak", "coder", "code", "parse", "retriever", "clip")
TIMEOUT = {"ollama": 240, "nvidia": 45}      # first Ollama call loads the model; NVIDIA free tier can queue
FALLBACK_MODELS = 2                           # extra models tried per provider before moving on

_GONE: set[str] = set()    # models that answered 404/410 this run
_PICKED: list = []          # cache: [{prov: resolved or None}]
_DEAD: set[str] = set()     # keys refused with 401/403 this run
_REST: dict[str, float] = {}  # key -> time it may be used again (after 429)
_TURN: dict[str, int] = {}  # round-robin start per provider


# ── keys ───────────────────────────────────────────────────────────────────
def keys(prov: str) -> list[str]:
    """All keys configured for a provider, in order, without duplicates."""
    if prov == "ollama":
        return []
    pinned = (env("LLM_PROVIDER", "auto") or "auto").lower() == prov
    name = KEY_ENV.get(prov, "")
    raw = [env("LLM_API_KEY")] if pinned else []
    raw += re.split(r"[,\s]+", env(name + "S", "") or "")
    raw += [env(name)] + [env(f"{name}_{i}") for i in range(2, 10)]
    out: list[str] = []
    for k in raw:
        k = (k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def mask(key: str | None) -> str:
    return "none" if not key else (key[:6] + "…" + key[-4:] if len(key) > 12 else "…" + key[-2:])


def _usable(prov: str) -> list[str]:
    ks = [k for k in keys(prov) if k not in _DEAD]
    if not ks:
        return []
    i = _TURN.get(prov, 0) % len(ks)
    ks = ks[i:] + ks[:i]                       # spread load across keys
    now = time.time()
    return [k for k in ks if _REST.get(k, 0) <= now]   # all resting → [] → next provider


# ── models ─────────────────────────────────────────────────────────────────
def _get_models(base: str, key: str | None) -> tuple[int, list[str] | None]:
    try:
        r = requests.get(base.rstrip("/") + "/models", timeout=8,
                         headers={"Authorization": f"Bearer {key}"} if key else {})
        if r.status_code >= 400:
            return r.status_code, None
        return 200, [str(m.get("id", "")).removeprefix("models/") for m in r.json().get("data", [])]
    except (requests.RequestException, ValueError, AttributeError):
        return 0, None


def _models(base: str, key: str | None) -> list[str] | None:
    return _get_models(base, key)[1]


def _reachable(base: str, key: str | None) -> bool:
    return _models(base, key) is not None


def _version(mid: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", mid)[:3])


def _ok_id(m: str) -> bool:
    low = m.lower()
    return not any(s in low for s in SKIP)


def ranked(prov: str, ids: list[str]) -> list[str]:
    """Live models in preference order (best first), chat-capable only."""
    out: list[str] = []
    for pat in PREFER.get(prov, [r"."]):
        hits = [m for m in ids if re.search(pat, m) and _ok_id(m) and m not in out]
        if prov == "gemini":
            hits.sort(key=_version, reverse=True)
        out += hits
    return out


def pick_model(prov: str, wanted: str, ids: list[str]) -> str:
    """`wanted` if the provider still serves it, else the best current equivalent."""
    if not ids or wanted in ids:
        return wanted
    r = ranked(prov, ids)
    return r[0] if r else wanted


def _resolve(prov: str) -> dict | None:
    """Base URL, model order and keys of a provider, or None if it can't be used now."""
    base, model = PRESETS.get(prov, PRESETS["ollama"])
    pinned = (env("LLM_PROVIDER", "auto") or "auto").lower() == prov
    base = env("LLM_BASE_URL", base) if pinned else base
    model = env(MODEL_ENV.get(prov, ""), None) or (env("LLM_MODEL", None) if pinned else None) or model
    ks = keys(prov)
    if prov != "ollama" and not ks:
        return None
    ids, good = None, []
    for k in (ks or [None]):
        code, ids = _get_models(base, k)
        if ids is not None:
            good.append(k)
            break
        if code in (401, 403) and k:
            _DEAD.add(k)
    if ids is None:
        return None
    first = pick_model(prov, model, ids)
    order = [first] + [m for m in ranked(prov, ids) if m != first][:FALLBACK_MODELS + 2]
    return {"prov": prov, "base": base, "models": order, "ids": ids}


def chain() -> list[str]:
    prov = (env("LLM_PROVIDER", "auto") or "auto").lower()
    if prov == "none":
        return []
    return list(CHAIN) if prov == "auto" else [prov]


def _providers() -> list[dict]:
    if not _PICKED:
        _PICKED.append({p: _resolve(p) for p in chain()})
    return [r for r in _PICKED[0].values() if r]


def settings() -> tuple[str, str, str | None]:
    """(base_url, model, first key) of the provider tried first."""
    ps = _providers()
    if not ps:
        return ("", "none", None)
    p = ps[0]
    ks = _usable(p["prov"])
    return p["base"], p["models"][0], (ks[0] if ks else None)


def available() -> bool:
    return bool(_providers())


def status() -> list[dict]:
    """One row per provider in the chain, for `hunt doctor` / `hunt llm` (no chat calls)."""
    _providers()
    rows = []
    for prov in chain():
        r = (_PICKED[0] or {}).get(prov)
        n = len(keys(prov))
        if r:
            note = f"{len(r['ids'])} models live"
        elif prov != "ollama" and not n:
            note = f"no key ({KEY_ENV.get(prov)}S)"
        elif prov == "ollama":
            note = "not running (sudo systemctl enable --now ollama)"
        else:
            note = "keys refused or endpoint unreachable"
        rows.append({"prov": prov, "ok": bool(r), "model": r["models"][0] if r else None,
                     "fallbacks": r["models"][1:] if r else [], "keys": n,
                     "dead": sum(1 for k in keys(prov) if k in _DEAD), "note": note})
    return rows


# ── chat ───────────────────────────────────────────────────────────────────
THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def clean(text: str | None) -> str:
    """Drop reasoning blocks (<think>…</think>); an unclosed one means no answer was produced."""
    t = THINK.sub("", text or "")
    if re.search(r"<think>", t, re.I):
        t = t[:re.search(r"<think>", t, re.I).start()]
    return t.strip()


def _system_for(prov: str, model: str, system: str) -> str:
    if prov == "ollama" and model.lower().startswith("qwen3"):
        return system + "\n/no_think"         # Qwen3's switch to skip its thinking phase
    return system


def _post(p: dict, model: str, key: str | None, system: str, user: str, temperature: float,
          max_tokens: int, timeout: float | None = None) -> tuple[int, str | None]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = requests.post(p["base"].rstrip("/") + "/chat/completions", headers=headers,
                          timeout=timeout or TIMEOUT.get(p["prov"], 120), json={
                              "model": model, "temperature": temperature, "max_tokens": max_tokens,
                              "stream": False,
                              "messages": [{"role": "system", "content": _system_for(p["prov"], model, system)},
                                           {"role": "user", "content": user}]})
        if not r.ok:
            return r.status_code, None
        return 200, clean(r.json()["choices"][0]["message"].get("content"))
    except requests.Timeout:
        return 408, None
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError, AttributeError):
        return 0, None


LAST: dict = {}   # provider/model/key that answered last (shown by `hunt llm --test`)


def chat(system: str, user: str, temperature: float = 0.3, max_tokens: int = 900) -> str | None:
    """Reply text, or None if every key, model and provider failed."""
    for p in _providers():
        prov = p["prov"]
        for model in [m for m in p["models"] if m not in _GONE]:
            ks = _usable(prov) if prov != "ollama" else [None]
            if prov != "ollama" and not ks:
                break                                   # every key dead or resting: next provider
            next_model = False
            for k in ks:
                code, text = _post(p, model, k, system, user, temperature, max_tokens)
                if code == 200 and text:
                    _TURN[prov] = _TURN.get(prov, 0) + 1
                    LAST.update(prov=prov, model=model, key=mask(k))
                    return text
                if code in (401, 403) and k:
                    _DEAD.add(k)                        # bad / revoked key: skip it from now on
                    continue
                if code == 429 and k:
                    _REST[k] = time.time() + 60         # rate-limited: rest this key, try the next
                    continue
                if code in (404, 410):
                    _GONE.add(model)                    # listed but retired (410 Gone): never again this run
                next_model = True                       # 404/410 model gone, 5xx, timeout, empty reply
                break
            if not next_model and prov != "ollama" and not _usable(prov):
                break
    return None
