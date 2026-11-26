"""Accuracy of the chat assistant's routing on a fixed eval set (tests/evals/chat.jsonl): right tool, right args?

run_chat(): each message through assistant.route (fast paths, else the model's JSON; nothing is run).
Live check against the real model:  .venv/bin/python -m jobhunter.evals   (target ≥ 95 %)
"""
from __future__ import annotations

import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

SETS = Path(__file__).resolve().parent.parent / "tests" / "evals"
TARGET = 95


def cases(name: str = "chat") -> list[dict]:
    return [json.loads(x) for x in (SETS / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


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
        for c in cases_ or cases():
            chat = f"eval{c['id']}"
            for role, text in c.get("history") or []:
                assistant.remember(db, chat, role, text)
            try:
                got = route(c["text"], db, chat)
            except Exception as exc:                 # a crash is a miss, never the end of the eval
                got = ("error", {"error": f"{type(exc).__name__}: {exc}"})
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


def report(chat: dict | None = None) -> str:
    """Per-category table and every miss."""
    chat = chat or run_chat()
    lines = [f"{'category':<10} {'right':>5} {'total':>5} {'acc %':>6}"]
    for k, v in chat["by_cat"].items():
        lines.append(f"{k:<10} {v['right']:>5} {v['total']:>5} {v['accuracy']:>6g}")
    lines.append(f"{'overall':<10} {chat['right']:>5} {chat['total']:>5} {chat['accuracy']:>6g}")
    lines += ["", "misses:"] + [f"  {m['id']}: {m['text']!r} want {m['want']} got {m['got']}" for m in chat["misses"]]
    return "\n".join(lines)


if __name__ == "__main__":
    r = run_chat()
    print(report(r))
    sys.exit(0 if r["accuracy"] >= TARGET else 1)
