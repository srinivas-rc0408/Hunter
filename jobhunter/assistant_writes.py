"""The chat assistant's change tools (w_*): each describes the change (run=False) for a "yes", then makes it
(run=True). assistant.py lists them in TOOLS and re-exports every name here."""
from __future__ import annotations

from .bot_views import _e
from .db import DB


# ── tools: changes (run only after "yes") ──────────────────────────────────
STATUS_WORDS = {"applied": "applied", "interview": "interview", "interviewing": "interview", "offer": "offer",
                "rejected": "rejected", "reject": "rejected", "skipped": "skipped", "skip": "skipped", "queued": "queued",
                "queue": "queued"}


class Nope(ValueError):
    """A change that can't be made as asked: the message says why."""


def _job_or_err(db: DB, id: str) -> dict:
    from .assistant import find_job      # at call time: assistant imports this module
    j = find_job(db, id)
    if not j:
        raise Nope(f"No job matches <i>{_e(id)}</i>. Send its 8-letter id from a card.")
    return j


def w_set_status(db: DB, id: str, status: str, run: bool = False):
    j = _job_or_err(db, id)
    st = STATUS_WORDS.get((status or "").lower().strip())
    if not st:
        raise Nope(f"I can mark a job as {', '.join(sorted(set(STATUS_WORDS.values())))}.")
    if not run:
        return f"mark <b>{_e(j['title'][:60])}</b> · {_e(j['company'])} as <b>{st}</b>"
    db.set_status(j["id"], st, "telegram chat")
    from .tracker import refresh
    refresh(db)
    out = f"✓ {_e(j['title'][:60])} · {_e(j['company'])} → <b>{st}</b>"
    if st == "interview":
        from .prep import pack
        return [out, pack(j)]
    return out


def w_add_note(db: DB, id: str, text: str, run: bool = False):
    j = _job_or_err(db, id)
    if not (text or "").strip():
        raise Nope("What should the note say?")
    if not run:
        return f"add the note “{_e(text.strip()[:200])}” to <b>{_e(j['title'][:60])}</b> · {_e(j['company'])}"
    db.note(j["id"], text.strip())
    from .tracker import refresh
    refresh(db)
    return f"✓ note added to {_e(j['title'][:60])} · {_e(j['company'])}"


def w_skip(db: DB, id: str, run: bool = False):
    return w_set_status(db, id, "skipped", run)


def _dream(db: DB, name: str, add: bool, run: bool):
    from . import dreams
    if not (name or "").strip():
        raise Nope("Which company?")
    if not run:
        return f"{'add' if add else 'remove'} <b>{_e(name)}</b> {'to' if add else 'from'} your dream companies"
    ok, msg = (dreams.add if add else dreams.remove)(name)
    if not ok:
        return "· " + _e(msg)
    return "✓ " + _e(msg) + "".join("\n• " + _e(n) for n in dreams.after_change(name, add))


def w_dream(db: DB, name: str, run: bool = False):
    return _dream(db, name, True, run)


def w_undream(db: DB, name: str, run: bool = False):
    return _dream(db, name, False, run)


def _sheet_job(db: DB, id: str) -> dict:
    j = _job_or_err(db, id)
    return j


def _on_sheet(j: dict) -> str:
    return "" if j["status"] in ("applied", "interview", "offer", "rejected") else " (it shows on the sheet once you apply)"


def w_sheet_add_column(db: DB, name: str, run: bool = False):
    from . import tracker
    name = " ".join((name or "").strip(" '\"“”‘’").split())
    if not name:
        raise Nope("What should the column be called?")
    if tracker.column_name(db, name) or name.lower() in {h.lower() for h in tracker.H + tracker.TAIL}:
        raise Nope(f"The sheet already has a column “{_e(name)}”.")
    if not run:
        return f"add a column <b>{_e(name)}</b> to the sheet (after the standard ones; it survives every rewrite)"
    tracker.add_column(db, name)
    tracker.refresh(db)
    return f"✓ column <b>{_e(name)}</b> added. Fill it: <i>put 'text' in {_e(name)} for ab12cd34</i>"


def w_sheet_set(db: DB, id: str, column: str, value: str, run: bool = False):
    from . import tracker
    j = _sheet_job(db, id)
    col = tracker.column_name(db, column)
    std = next((h for h in tracker.H + tracker.TAIL if h.lower() == (column or "").strip().lower()), None)
    if not col and std:
        raise Nope(f"<b>{_e(std)}</b> is filled from the job record on every rewrite, so I can't type into it. "
                   f"For status say <i>mark {_e(id)} as interview</i>; for anything else, add your own column.")
    if not col:
        raise Nope(f"There's no column “{_e(column)}” in the sheet. Say <i>add a column {_e(column)}</i> first.")
    value = (value or "").strip()
    if not run:
        return (f"put “{_e(value)}” in <b>{_e(col)}</b> for {_e(j['title'][:60])} · {_e(j['company'])}" + _on_sheet(j)
                if value else f"clear <b>{_e(col)}</b> for {_e(j['title'][:60])} · {_e(j['company'])}")
    tracker.set_value(db, j["id"], col, value)
    tracker.refresh(db)
    return f"✓ {_e(col)} for {_e(j['title'][:60])} · {_e(j['company'])}: “{_e(value)}”"


def w_sheet_remove_row(db: DB, id: str, run: bool = False):
    from . import tracker
    j = _sheet_job(db, id)
    if not run:
        return (f"hide <b>{_e(j['title'][:60])}</b> · {_e(j['company'])} from the sheet (the job and your application "
                "record stay; <i>show hidden</i> brings it back)")
    tracker.hide(db, j["id"])
    tracker.refresh(db)
    return f"✓ {_e(j['title'][:60])} · {_e(j['company'])} hidden from the sheet. <i>show hidden</i> brings it back."


def w_sheet_show_hidden(db: DB, run: bool = False):
    from . import tracker
    tracker.ensure(db)
    n = db.c.execute("SELECT COUNT(*) FROM sheet_hidden").fetchone()[0]
    if not n:
        raise Nope("No rows are hidden.")
    if not run:
        return f"put the {n} hidden row{'s' if n > 1 else ''} back on the sheet"
    tracker.show_hidden(db)
    tracker.refresh(db)
    return f"✓ {n} row{'s' if n > 1 else ''} back on the sheet"


# name → (function, changes something?, one-line description for the model)
