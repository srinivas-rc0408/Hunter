"""Follow-up suggestions: 7 days after you applied with no reply, Telegram gets a short polite draft
(skills/hunter-followup's application-check-in template) and a LinkedIn search for the company's recruiters.
Nothing is ever sent to the company: you copy, edit the [fill: …] parts, and send it yourself."""
from __future__ import annotations

import datetime as dt
import html
import importlib.util
from urllib.parse import quote

from . import config
from .db import DB
from .notify import telegram

DAYS = 7


def _skill():
    spec = importlib.util.spec_from_file_location("hunter_followup", config.path("skills", "hunter-followup", "followup.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def due(db: DB, today: dt.date | None = None) -> list[dict]:
    """Still "applied" (no interview / rejection / offer) DAYS+ days on, and never suggested before."""
    cut = str((today or dt.date.today()) - dt.timedelta(days=DAYS))
    done = {r[0] for r in db.c.execute("SELECT job_id FROM events WHERE kind='followup_suggested'")}
    return [j for j in db.query(status=["applied"], limit=100000)
            if (j.get("status_at") or "9")[:10] <= cut and j["id"] not in done]


def message(j: dict) -> str:
    f = _skill()
    t = f.load_templates()["application-check-in"]
    body, _ = f.fill(t["body"], {"role": j["title"], "company": j["company"], "applied": (j.get("status_at") or "")[:10],
                                 "my_name": config.cfg("profile").get("name")})
    link = "https://www.linkedin.com/search/results/people/?keywords=" + quote(f'{j["company"]} recruiter talent acquisition')
    e = lambda s: html.escape(str(s), quote=False)
    return (f"📬 <b>Follow up?</b> {e(j['title'][:70])} · {e(j['company'])}: applied {e((j.get('status_at') or '')[:10])}, "
            f"no reply in {DAYS} days.\nDraft (edit every [fill: …], then send it yourself; nothing is sent for you):\n"
            f"<pre>{e(body)}</pre>\n🔎 <a href=\"{html.escape(link, quote=True)}\">Find their recruiter on LinkedIn</a>\n"
            f"Got a reply? /interview_{j['id'][:8]} · /rejected_{j['id'][:8]}")


def suggest(db: DB | None = None, today: dt.date | None = None) -> int:
    """Send each due suggestion once (an event row marks it). Returns how many went out."""
    db = db or DB()
    n = 0
    for j in due(db, today):
        if telegram(message(j)):
            db.event(j["id"], "followup_suggested", "telegram")
            n += 1
    return n
