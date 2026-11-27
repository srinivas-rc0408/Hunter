"""Accuracy of the chat assistant's routing on a fixed eval set (tests/evals/chat.jsonl): right tool, right args?

run_chat(): each message through assistant.route (fast paths, else the model's JSON; nothing is run).
Live check against the real model:  .venv/bin/python -m jobhunter.evals   (target ≥ 95 %)
"""
from __future__ import annotations

import json
import re
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


# ── whole answers on your real jobs (data/evals/answers.jsonl: it names live job ids, so it isn't committed) ──
ANSWERS = Path(__file__).resolve().parent.parent / "data" / "evals" / "answers.jsonl"
NONE = re.compile(r"not (?:found|in the (?:posting|data|jobs)|listed|mentioned|stated|given|specified|available)|"
                  r"doesn'?t (?:say|mention|list|state)|does not (?:say|mention|list|state)|no (?:open )?(?:jobs?|matches|deadline|"
                  r"information|details)|couldn'?t find|could not find|isn'?t (?:listed|mentioned|stated)|nothing (?:matches|found)", re.I)
HEX = re.compile(r"(?<![0-9a-z])([0-9a-f]{8})[0-9a-f]{0,4}(?![0-9a-z])")


def _plain(html_: str) -> str:
    return re.sub(r"<[^>]+>", "", html_).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def score_answer(c: dict, msgs: list[str], known) -> dict:
    """accurate: every expected group found (or an honest "not in the data" with nothing forbidden);
    cited: the job ids it names are real, and the right ones; cards: the cards shown are exactly the jobs the answer names."""
    from .outcomes import CARD_ID
    text = _plain("\n\n".join(msgs)).lower()
    first = _plain(msgs[0] if msgs else "")
    cards = {m[:8] for x in msgs for m in CARD_ID.findall(x)}
    named = {m for m in HEX.findall(first.lower()) if known(m)}
    cited = {m for m in HEX.findall(text) if known(m)} | cards
    fake = {m for m in HEX.findall(text) if not known(m)}
    said = first.split("\n\n")[0].lower()           # the answer block: not the cards / labelled estimate packed after it
    bad = [f for f in c.get("forbid", []) if f.lower() in said]
    if c.get("none"):
        ok = bool(NONE.search(text)) and not bad
    else:
        ok = all(any(a.lower() in text for a in g) for g in c["expect"]) and not bad
    want = set(c.get("cite") or [])
    if not want:
        cite_ok = not fake
    elif c.get("optional"):
        cite_ok = not fake and cited <= want
    elif c.get("any"):
        cite_ok = not fake and bool(cited & want) and cited <= want
    else:
        cite_ok = not fake and want <= cited and cited <= want
    return {"ok": ok, "cite_ok": cite_ok, "cards_ok": not cards or named == cards,
            "cited": sorted(cited), "cards": sorted(cards), "forbidden": bad}


def run_answers(path: Path = ANSWERS, handle=None) -> dict:
    """Each question through the bot's real path (bot.handle) on the real DB, timed; chat history is wiped per case."""
    import statistics
    import time
    from . import assistant, bot
    from .db import DB
    db = DB()
    assistant.ensure(db)
    known = lambda pre: db.c.execute("SELECT 1 FROM jobs WHERE id LIKE ?", (pre + "%",)).fetchone() is not None  # noqa: E731
    handle = handle or bot.handle
    rows = []
    for c in cases_at(path):
        chat = f"eval-{c['id']}"
        for t in ("chat_turns", "chat_pending"):
            db.c.execute(f"DELETE FROM {t} WHERE chat=?", (chat,))
        db.c.commit()
        assistant._CACHE.clear()
        t0 = time.time()
        try:
            out = handle(c["text"], db=db, chat=chat, progress=lambda s: None)
        except Exception as exc:                     # a crash is a miss, never the end of the eval
            out = f"error: {type(exc).__name__}: {exc}"
        secs = time.time() - t0
        msgs = [out] if isinstance(out, str) else list(out)
        rows.append({**c, **score_answer(c, msgs, known), "secs": round(secs, 2), "reply": _plain(msgs[0] if msgs else "")[:300], "msgs": msgs,
                     "model": dict(__import__("jobhunter.llm", fromlist=["LAST"]).LAST)})
        for t in ("chat_turns", "chat_pending"):
            db.c.execute(f"DELETE FROM {t} WHERE chat=?", (chat,))
        db.c.commit()
    secs = sorted(r["secs"] for r in rows)
    pct = lambda k: round(100 * sum(r[k] for r in rows) / max(len(rows), 1), 1)  # noqa: E731
    return {"n": len(rows), "accuracy": pct("ok"), "citations": pct("cite_ok"), "cards": pct("cards_ok"),
            "median": round(statistics.median(secs), 2), "p95": secs[min(len(secs) - 1, int(0.95 * len(secs)))], "rows": rows}


def cases_at(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def answers_report(r: dict) -> str:
    by = defaultdict(list)
    for x in r["rows"]:
        by[x["cat"]].append(x)
    lines = [f"{'category':<9} {'n':>3} {'accurate':>9} {'cited':>6} {'median s':>9}"]
    import statistics
    for k, xs in by.items():
        lines.append(f"{k:<9} {len(xs):>3} {sum(x['ok'] for x in xs):>9} {sum(x['cite_ok'] for x in xs):>6} "
                     f"{statistics.median(x['secs'] for x in xs):>9.1f}")
    lines.append(f"overall: accuracy {r['accuracy']}% · citations {r['citations']}% · cards = cited {r['cards']}% · "
                 f"median {r['median']} s · p95 {r['p95']} s")
    lines += ["", "misses:"] + [f"  {x['id']} [{'' if x['ok'] else 'wrong '}{'' if x['cite_ok'] else 'cite'}] {x['text']!r} "
                                f"cited {x['cited']} → {x['reply'][:160]!r}" for x in r["rows"] if not (x["ok"] and x["cite_ok"])]
    return "\n".join(lines)


def rescore(r: dict, path: Path = ANSWERS) -> dict:
    """Score saved replies again (same replies, current cases and rules): no model calls."""
    import statistics
    from .db import DB
    db = DB()
    known = lambda pre: db.c.execute("SELECT 1 FROM jobs WHERE id LIKE ?", (pre + "%",)).fetchone() is not None  # noqa: E731
    cs = {c["id"]: c for c in cases_at(path)}
    rows = [{**x, **cs[x["id"]], **score_answer(cs[x["id"]], x["msgs"], known)} for x in r["rows"]]
    secs = sorted(x["secs"] for x in rows)
    pct = lambda k: round(100 * sum(x[k] for x in rows) / len(rows), 1)  # noqa: E731
    return {**r, "rows": rows, "accuracy": pct("ok"), "citations": pct("cite_ok"), "cards": pct("cards_ok"),
            "median": round(statistics.median(secs), 2), "p95": secs[min(len(secs) - 1, int(0.95 * len(secs)))]}


if __name__ == "__main__":
    # python -m jobhunter.evals answers [out.json]               ask the 30 questions, save the replies
    # python -m jobhunter.evals answers --rescore in.json out.json   score saved replies again (no model calls)
    if sys.argv[1:2] == ["answers"]:
        if sys.argv[2:3] == ["--rescore"]:
            r, out = rescore(json.loads(Path(sys.argv[3]).read_text())), Path(sys.argv[4])
        else:
            r, out = run_answers(), Path(sys.argv[2]) if len(sys.argv) > 2 else ANSWERS.with_name("last.json")
        out.write_text(json.dumps(r, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        print(answers_report(r))
        sys.exit(0)
    r = run_chat()
    print(report(r))
    sys.exit(0 if r["accuracy"] >= TARGET else 1)
