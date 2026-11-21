"""Morning brief: one screen (and one Telegram message) that tells you what to do today.

  ☁ cloud radar heartbeat   is the 30-min GitHub scan alive? (stale → exact fix)
  ★ best new jobs           since your last brief, dream companies first
  ✋ waiting for you         forms queued in the Review Center
  🏛 govt deadlines          eligible/check notices closing in the next 10 days
  📅 calendar               GATE countdown + the next dated event
  🎯 today's target         applications to reach your daily goal

Runs at 07:30 from a systemd timer (`hunt morning --send`); run it by hand any time.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import subprocess

from . import config
from .config import env
from .db import DB
from .notify import job_card, pages, telegram
from .util import iso

DAILY_GOAL = 10


def _state_file():
    return config.data_dir() / "state.json"


def load_state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


def save_state(**kw) -> None:
    st = load_state()
    st.update(kw)
    _state_file().write_text(json.dumps(st, indent=2))


def cloud_heartbeat() -> tuple[str, float | None]:
    """(text, hours since the cloud radar last saved state)."""
    try:
        subprocess.run(["git", "-C", str(config.home()), "fetch", "-q", "origin", "radar-state"],
                       check=True, timeout=40, capture_output=True)
        out = subprocess.run(["git", "-C", str(config.home()), "log", "-1", "--format=%ct", "origin/radar-state"],
                             check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        hrs = (dt.datetime.now().timestamp() - int(out)) / 3600
    except (subprocess.SubprocessError, ValueError, OSError):
        return "cloud radar: no state found (push the repo + `hunt secrets --push`, then `gh workflow run radar`)", None
    if hrs <= 1.5:
        return f"cloud radar alive (last run {hrs * 60:.0f} min ago)", hrs
    return (f"cloud radar STALE: last run {hrs:.1f} h ago. Fix: `gh workflow enable radar && gh workflow run radar`, "
            f"then check `gh run list -w radar`"), hrs


def gate_countdown(today: dt.date) -> str | None:
    gates = config.cfg("profile").get("exams", {}).get("gate", []) or []
    for g in gates:
        start = str(g.get("exam_window", "")).split("..")[0]
        try:
            d = dt.date.fromisoformat(start)
        except ValueError:
            continue
        if d >= today:
            return f"GATE {g.get('paper')} {g.get('year')} starts in {(d - today).days} days ({d:%d %b})"
    return None


def next_event(today: dt.date) -> str | None:
    evs = []
    for e in config.cfg("govt").get("calendar", []) or []:
        d = e["date"] if isinstance(e["date"], dt.date) else dt.date.fromisoformat(str(e["date"]))
        if d >= today:
            evs.append((d, e["event"]))
    if not evs:
        return None
    d, ev = min(evs)
    return f"{d:%d %b} ({(d - today).days} d): {ev}"


def build(db: DB | None = None, today: dt.date | None = None, heartbeat: bool = True) -> dict:
    db = db or DB()
    today = today or dt.date.today()
    st = load_state()
    since = st.get("last_brief") or (dt.datetime.now() - dt.timedelta(days=1)).isoformat(timespec="seconds")
    new = db.query(status=["new", "alerted", "queued"], kind="private", min_score=50, since=since, limit=60)
    new.sort(key=lambda j: (not j["dream"], -j["score"]))
    best = [] if new else db.query(status=["new", "alerted", "queued"], kind="private", min_score=50, limit=5)
    open_q = db.count("kind='private' AND status IN ('new','alerted','queued') AND score >= 50")
    waiting = db.query(status=["needs_you"], limit=50)
    soon = []
    for j in db.query(kind="govt", limit=500):
        if j["status"] in ("applied", "skipped", "rejected") or not j.get("deadline"):
            continue
        if "eligibility:ineligible" in (j.get("flags") or []):
            continue
        try:
            d = dt.date.fromisoformat(j["deadline"])
        except ValueError:
            continue
        if 0 <= (d - today).days <= 10:
            soon.append((d, j))
    soon.sort(key=lambda x: x[0])
    from .notify import govt_open
    govt_top = govt_open([j for j in db.query(kind="govt", limit=1000)
                          if "eligibility:ineligible" not in (j.get("flags") or [])], today)[:3]
    applied_today = db.applied_today()
    hb = cloud_heartbeat() if heartbeat else ("", None)
    from .apply.questions import pending
    qs = pending()
    return {"date": today.isoformat(), "since": since, "new": new, "best": best, "open": open_q, "waiting": waiting,
            "govt_soon": soon, "govt_top": govt_top, "applied_today": applied_today, "goal": DAILY_GOAL,
            "interviews": db.query(status=["interview"], limit=20), "heartbeat": hb,
            "gate": gate_countdown(today), "next_event": next_event(today), "questions": qs}


def as_telegram_pages(b: dict) -> list[str]:
    """The brief as whole Telegram messages: every job is a full card, never cut between messages."""
    e = lambda s: html.escape(str(s or ""), quote=False)
    head = f"<b>☀️ Morning brief · {dt.date.fromisoformat(b['date']):%a %d %b}</b>"
    top = []
    if b["heartbeat"][0]:
        top.append(("☁ " if (b["heartbeat"][1] or 99) <= 1.5 else "⚠️ ") + e(b["heartbeat"][0]))
    top.append(f"<b>★ New since last brief: {len(b['new'])}</b> (open queue {b['open']})"
               + ("" if b["new"] or not b.get("best") else "\nnothing new: your best open jobs (more: /top 20)"))
    blocks = ["\n".join(top)] + [job_card(j) for j in (b["new"] or b.get("best") or [])[:8]]
    if len(b["new"]) > 8:
        blocks.append(f"… and {len(b['new']) - 8} more: /top 20")
    if b["waiting"]:
        blocks.append(f"<b>✋ Waiting for your approval: {len(b['waiting'])}</b> (Review Center: localhost:8765)\n"
                      + "\n".join(f"• {e(j['title'])} · {e(j['company'])}" for j in b["waiting"][:5]))
    if b.get("questions"):
        q = b["questions"]
        blocks.append(f"<b>❓ {len(q)} question(s) only you can answer</b> (unblocks "
                      f"{len({j for r in q for j in r.get('jobs', [])})} job(s)): /questions")
    if b.get("govt_top"):
        from .notify import govt_card
        today = dt.date.fromisoformat(b["date"])
        blocks.append(f"<b>🏛 Top govt notices</b> (all: /govt · next 14 days: /deadlines)")
        blocks += [govt_card(j, today=today) for j in b["govt_top"]]
        rest = [j for _, j in b["govt_soon"] if j["id"] not in {g["id"] for g in b["govt_top"]}]
        if rest:
            blocks.append("<b>Also closing in 10 days</b>\n" + "\n".join(
                f"• {e(j['deadline'])} · {e(j['title'][:70])} · {e(j['company'])} /show_{j['id'][:8]}" for j in rest[:5]))
    tail = []
    if b["interviews"]:
        tail.append(f"<b>🎤 Interviews in progress: {len(b['interviews'])}</b>: prep with the interview drill")
    if b["gate"]:
        tail.append(f"📚 {e(b['gate'])}")
    if b["next_event"]:
        tail.append(f"📅 {e(b['next_event'])}")
    left = max(0, b["goal"] - b["applied_today"])
    tail.append(f"🎯 Today: {left} applications to your goal of {b['goal']} → <code>hunt apply --top {max(left, 5)}</code>")
    blocks.append("\n".join(tail))
    ps = pages(head, blocks)
    return [p.replace(head, head + f" ({i}/{len(ps)})", 1) if len(ps) > 1 else p for i, p in enumerate(ps, 1)]


def as_telegram(b: dict) -> str:
    return "\n\n".join(as_telegram_pages(b))


def run(send: bool = False, heartbeat: bool = True) -> dict:
    from .util import console
    b = build(heartbeat=heartbeat)
    msgs = as_telegram_pages(b)
    plain = html.unescape(re.sub(r"<[^>]+>", "", "\n\n".join(msgs)))
    console.print(plain, markup=False)
    if send:
        if not (env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID")):
            console.print("[yellow]Telegram not configured: brief printed only[/]")
        elif not all([telegram(m) for m in msgs]):   # offline (laptop just woke): exit 1, systemd retries
            console.print("[red]Telegram unreachable: brief NOT sent (will retry)[/]")
            raise SystemExit(1)                      # last_brief untouched: nothing new is skipped
    save_state(last_brief=iso())
    return b
