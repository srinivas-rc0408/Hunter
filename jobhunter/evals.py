"""Accuracy of the chat assistant's routing and of form-question reading, on fixed eval sets (tests/evals/*.jsonl).

run_chat(): each message through assistant.route (fast paths, else the model's JSON; nothing is run) → right tool
and args? run_forms(): each screening question through fields.classify / fields._understand → right key, never a value.
drift_check(): both against the live model; chat accuracy down ≥ 5 points → the next model that scores within 5 of
the best is saved as the assistant's model order (data/evals.json, read by assistant._models).
"""
from __future__ import annotations

import json
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from . import config, llm

SETS = Path(__file__).resolve().parent.parent / "tests" / "evals"
DROP = 5


def cases(name: str) -> list[dict]:
    return [json.loads(x) for x in (SETS / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def _file() -> Path:
    return config.data_dir() / "evals.json"


def saved() -> dict:
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    _file().write_text(json.dumps(st, indent=1))


def _match(want: dict, got: dict) -> bool:
    """Every expected arg present: strings as a case-insensitive substring, the rest as equal text."""
    for k, v in want.items():
        g = (got or {}).get(k)
        if g is None or (str(v).lower() not in str(g).lower() if isinstance(v, str) else str(v) != str(g)):
            return False
    return True


def run_chat(cases_: list[dict] | None = None, reply_fn=None, models: list[str] | None = None) -> dict:
    """reply_fn(text, db, chat) -> (tool, args) | None; default assistant.route with `models`."""
    from . import assistant
    from .db import DB
    route = reply_fn or (lambda t, db, chat: assistant.route(t, db, chat, models))
    with tempfile.TemporaryDirectory() as tmp:
        db = DB(Path(tmp) / "eval.db")              # never the real DB: follow-ups get their own chat history
        assistant.ensure(db)
        by, misses = defaultdict(lambda: [0, 0]), []
        for c in cases_ or cases("chat"):
            chat = f"eval{c['id']}"
            for role, text in c.get("history") or []:
                assistant.remember(db, chat, role, text)
            try:
                got = route(c["text"], db, chat)
            except Exception as exc:                 # a crash is a miss, never the end of the eval
                got = ("error", {"error": type(exc).__name__})
            ok = bool(got) and got[0] == c["tool"] and (c["tool"] == "none" or _match(c.get("args") or {}, got[1]))
            by[c["cat"]][0] += ok
            by[c["cat"]][1] += 1
            if not ok:
                misses.append({"id": c["id"], "text": c["text"], "want": [c["tool"], c.get("args")], "got": got})
        db.c.close()
    right, total = sum(v[0] for v in by.values()), sum(v[1] for v in by.values())
    return {"accuracy": round(100 * right / max(total, 1), 1), "right": right, "total": total,
            "by_cat": {k: {"right": a, "total": n, "accuracy": round(100 * a / n, 1)} for k, (a, n) in sorted(by.items())},
            "misses": misses}


def run_forms(cases_: list[dict] | None = None) -> dict:
    """right_field: the expected key (private questions: 'diversity' or 'unknown'). never_invents: the output is a
    RULES key or 'unknown', never a value, and a private question never lands on a profile field."""
    from .apply import fields
    keys = {k for k, _ in fields.RULES} | {"unknown", "cover_letter_file"}
    fields._UNDERSTOOD.clear()                       # each run asks the model afresh
    right = honest = 0
    misses = []
    cs = cases_ or cases("forms")
    for c in cs:
        key = fields.classify(c["q"], c.get("type", "text"))
        if key == "unknown":
            key = fields._understand(c["q"], c.get("options"))
        private_ok = not c.get("private") or key in ("diversity", "unknown")
        good = key == c["field"] or (c.get("private") and key in ("diversity", "unknown"))
        right += good
        honest += key in keys and private_ok
        if not good:
            misses.append({"q": c["q"], "want": c["field"], "got": key})
    n = max(len(cs), 1)
    return {"right_field": round(100 * right / n, 1), "never_invents": round(100 * honest / n, 1), "total": len(cs),
            "misses": misses}


def _order() -> list[str]:
    """Models to try, best guess first: the assistant's order, then NVIDIA's live fallbacks."""
    from . import assistant
    out = list(assistant._models()) + [m for m in assistant.MODELS]
    for p in llm._providers(("nvidia",)):
        out += p["models"]
    return list(dict.fromkeys(out))


def drift_check() -> tuple[bool, str]:
    """(ok, text for Telegram). Only with a live model; saves scores to data/evals.json."""
    if not llm.available():
        return True, "evals skipped: no live model"
    st = saved()
    order = _order()
    model = order[0]
    cur = run_chat(models=[model])["accuracy"]
    forms = run_forms()
    scores = st.setdefault("chat", {})
    last = scores.get(model, st.get("last"))
    best = max([cur, *scores.values()])
    scores[model] = cur
    st.update(last=cur, best=best, forms=forms["right_field"], never_invents=forms["never_invents"], at=time.time())
    text = f"chat {cur:g}% on {model}, forms {forms['right_field']:g}% (never invents {forms['never_invents']:g}%)"
    ok = forms["never_invents"] == 100
    if last is not None and last - cur >= DROP:
        tried = []
        for m in order[1:]:
            acc = run_chat(models=[m])["accuracy"]
            scores[m] = acc
            tried.append(f"{m} {acc:g}%")
            if acc >= best - DROP:
                st["models"] = [m] + [x for x in order if x != m]
                st["last"] = acc
                _save(st)
                return False, f"⚠ chat accuracy fell {last:g}→{cur:g}% on {model}; switched to {m} ({acc:g}%)"
        _save(st)
        return False, f"⚠ chat accuracy fell {last:g}→{cur:g}% on {model}; no model within {DROP} points ({', '.join(tried)})"
    _save(st)
    return ok, ("" if ok else "⚠ ") + text


def report(chat: dict | None = None, forms: dict | None = None) -> str:
    """Tables for `hunt eval` (and the report): per-category chat accuracy, form metrics, misses."""
    chat = chat or run_chat()
    forms = forms or run_forms()
    lines = [f"{'category':<10} {'right':>5} {'total':>5} {'acc %':>6}"]
    for k, v in chat["by_cat"].items():
        lines.append(f"{k:<10} {v['right']:>5} {v['total']:>5} {v['accuracy']:>6g}")
    lines.append(f"{'overall':<10} {chat['right']:>5} {chat['total']:>5} {chat['accuracy']:>6g}")
    lines += ["", f"forms: right_field {forms['right_field']:g}% · never_invents {forms['never_invents']:g}% "
                  f"· {forms['total']} questions"]
    lines += ["", "chat misses:"] + [f"  {m['id']}: {m['text']!r} want {m['want']} got {m['got']}" for m in chat["misses"]]
    lines += ["form misses:"] + [f"  {m['q']!r} want {m['want']} got {m['got']}" for m in forms["misses"]]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
