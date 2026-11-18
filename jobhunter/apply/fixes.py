"""Correct one field of a recorded application, from Telegram (/fix) or the Review Center.

The new value replaces the old one in the record, and is saved as YOUR answer for that
question: it wins over the bot's own choice on every future form ("never the same mistake twice").
"""
from __future__ import annotations

from ..db import DB
from . import fields as F
from .panel import NEVER_LEARN


def latest(db: DB, jid: str) -> tuple[dict | None, dict | None]:
    j = db.get(jid) if jid else None
    if not j:
        return None, None
    apps = db.applications(j["id"], limit=1)
    return j, (apps[0] if apps else None)


def fix(db: DB, jid: str, n: int, value: str) -> tuple[bool, str]:
    j, app = latest(db, jid)
    if not j:
        return False, "no such job"
    if not app or not app.get("fields"):
        return False, "nothing recorded for this job yet: fill it first (preview)"
    fs = app["fields"]
    if not 1 <= n <= len(fs):
        return False, f"field number must be 1-{len(fs)}"
    f = fs[n - 1]
    value = value.strip()
    if not value:
        return False, "type the new value after the number"
    opts = f.get("options") or []
    if opts:
        pick = F.pick_option(value, opts)
        if not pick:
            return False, "that isn't one of the choices: " + " | ".join(opts[:12])
        value = pick
    if f.get("type") == "checkbox":
        yes = F.pick_option(value, ["Yes", "No"])
        if not yes:
            return False, "for a tick box, answer yes or no"
        value = yes == "Yes"
    before = f.get("value")
    f.update(value=value, basis="you", basic=True, note=f"you changed it from {before if before not in (None, '') else 'empty'}")
    db.set_app_fields(app["n"], fs)
    label = f.get("label") or ""
    if f.get("key") in NEVER_LEARN or not label:
        return True, f"changed for this job only ({label or f.get('key')} isn't saved as a general answer)"
    F.learn(label, value, app.get("site") or "", override=True)
    if j["status"] in ("new", "alerted", "queued"):
        db.set_status(j["id"], "needs_you", "field corrected")
    return True, f"{label[:60]} → {value}. Saved as your answer: used on every future form."
