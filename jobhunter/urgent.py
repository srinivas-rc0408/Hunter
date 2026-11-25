"""🚨 APPLY NOW: a job scoring alerts.urgent_score (95) or more gets its own pinned Telegram message.

At most alerts.urgent_per_day (3) a day; the rest go out as normal alerts. On the laptop the job is queued first
for `hunt apply` (which still waits for your Submit). Reminders after 2 h and 6 h while it's neither applied nor
skipped, then silence. The pin comes off once you apply or skip it.
State lives in `events` (urgent, urgent_reminded, urgent_done), so cloud-sent alerts merge into the laptop DB.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from . import config, notify
from .db import DB
from .util import IST, iso, parse_iso

REMIND_H = (2, 6)
SETTLED = ("applied", "interview", "offer", "rejected", "skipped", "expired")


def _cfg() -> tuple[int, int]:
    a = config.cfg("search").get("alerts") or {}
    return int(a.get("urgent_score", 95)), int(a.get("urgent_per_day", 3))


def sent_today(db: DB, now: dt.datetime | None = None) -> int:
    day = (now or dt.datetime.now(IST)).astimezone(IST).date()
    return sum(1 for (at,) in db.c.execute("SELECT at FROM events WHERE kind='urgent'")
               if (d := parse_iso(at)) and d.astimezone(IST).date() == day)


def split(db: DB, jobs: list[dict], now: dt.datetime | None = None) -> tuple[list[dict], list[dict]]:
    """(urgent, normal): private jobs ≥ urgent_score, best first, up to today's cap left; everything else is normal."""
    score, cap = _cfg()
    left = max(0, cap - sent_today(db, now))
    hot = sorted((j for j in jobs if j.get("kind") != "govt" and (j.get("score") or 0) >= score),
                 key=lambda j: -(j.get("score") or 0))[:left]
    ids = {j["id"] for j in hot}
    return hot, [j for j in jobs if j["id"] not in ids]


def _ago(j: dict, now: dt.datetime) -> str:
    p = parse_iso(j.get("posted_at") or "") or parse_iso(j.get("found_at") or "")
    if not p:
        return "posted: date not listed"
    if len(j.get("posted_at") or "") <= 10 and j.get("posted_at"):
        days = (now.date() - p.date()).days
        return "posted today" if days <= 0 else f"posted {days} day{'s' * (days != 1)} ago"
    h = max(0, int((now - p.astimezone(IST)).total_seconds() // 3600))
    word = "posted" if j.get("posted_at") else "found"
    return f"{word} {h} h ago" if h < 48 else f"{word} {h // 24} days ago"


def text(j: dict, db: DB | None = None, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(IST)
    jid = j["id"][:8]
    why = "\n".join("• " + notify._esc(r[:140]) for r in (j.get("reasons") or [])[:6]) or "• (no reasons stored)"
    return (f"🚨 <b>APPLY NOW</b> (score {j.get('score', 0)}) · {_ago(j, now)}\n\n{notify.job_card(j, db)}\n\n"
            f"<b>Why it matched</b>\n{why}\n\n📱 /check_{jid} · 💻 /laptop_{jid}")   # the card has the apply link


def send(db: DB, j: dict, laptop: bool | None = None) -> bool:
    """Send + pin; on the laptop, queue it first for `hunt apply`. True if Telegram took it."""
    laptop = (not os.environ.get("GITHUB_ACTIONS")) if laptop is None else laptop
    mid = notify.telegram_id(text(j, db))
    if mid:
        notify.tg("pinChatMessage", message_id=mid, disable_notification=False)
    db.event(j["id"], "urgent", json.dumps({"mid": mid, "score": j.get("score")}))
    db.set_status(j["id"], "queued" if laptop else "alerted", "urgent alert" + (": queued first for hunt apply" if laptop else ""))
    return bool(mid)


def tick(db: DB | None = None, now: dt.datetime | None = None) -> int:
    """Unpin what you applied to / skipped; remind at 2 h and 6 h, then stop. Returns messages sent."""
    db = db or DB()
    now = now or dt.datetime.now(IST)
    sent = 0
    open_ = db.c.execute("""SELECT job_id, MIN(at) AS at, MAX(detail) AS detail FROM events WHERE kind='urgent'
                            AND job_id NOT IN (SELECT job_id FROM events WHERE kind='urgent_done') GROUP BY job_id""")
    for jid, at, detail in open_.fetchall():
        j = db.get(jid)
        mid = (json.loads(detail or "{}") or {}).get("mid")
        if not j or j["status"] in SETTLED:
            if mid:
                notify.tg("unpinChatMessage", message_id=mid)
            db.event(jid, "urgent_done", j["status"] if j else "gone")
            continue
        done = db.c.execute("SELECT COUNT(*) FROM events WHERE job_id=? AND kind='urgent_reminded'", (jid,)).fetchone()[0]
        start = parse_iso(at)
        if done >= len(REMIND_H) or not start:
            continue
        due = sum((now - start).total_seconds() >= h * 3600 for h in REMIND_H)
        if due > done:                             # laptop was off past 6 h: one reminder, not two in a row
            notify.telegram(f"⏰ <b>Still open</b> ({REMIND_H[due - 1]} h): {notify._esc(j['title'][:70])} · "
                            f"{notify._esc(j['company'])} (score {j.get('score', 0)})\n"
                            f"📱 /check_{jid[:8]} · 💻 /laptop_{jid[:8]} · ✅ /applied_{jid[:8]} · /skip_{jid[:8]}"
                            + ("" if due < len(REMIND_H) else "\nLast reminder."))
            for _ in range(due - done):
                db.event(jid, "urgent_reminded", iso())
            sent += 1
    return sent
