"""What happened to each application, and what that says.

• Buttons: any Telegram message holding one job card gets buttons for its next outcome (✅ Applied → 🎤 Interview /
  ✗ Rejected / 👻 Ghosted → 🎉 Offer). A tap records it like /applied_<id> does, and the buttons move on.
• No reply in `ghost_after_days`: one message asks "ghosted?" (with the same buttons and ⏳ Still waiting).
• /outcomes and the weekly report: response and interview rate by source, by role and by week.
• Once there are `learn_after` decided outcomes, jobs that look like the ones that got interviews rank higher.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import math
import re
from collections import Counter, defaultdict

from . import config
from .db import DB

ANSWERED = ("interview", "offer", "rejected")          # a real reply (ghosted and waiting are not)
WON = ("interview", "offer")
DECIDED = ("interview", "offer", "rejected", "ghosted")
LABEL = {"applied": "✅ Applied", "skipped": "⏭ Skip", "interview": "🎤 Interview", "offer": "🎉 Offer",
         "rejected": "✗ Rejected", "ghosted": "👻 Ghosted", "wait": "⏳ Still waiting"}
NEXT = {"applied": ("interview", "rejected", "ghosted"), "interview": ("offer", "rejected", "ghosted"),
        "ghosted": ("interview", "rejected"), "rejected": ("interview",), "offer": (), "skipped": ("applied",),
        "needs_you": (), "filled": ()}         # a form waiting for your approval: approve or skip it, no ✅ Applied
FRESH = ("applied", "skipped")                         # a job you haven't acted on yet
CARD_ID = re.compile(r"/check_([0-9a-f]{8,12})\b")


def _cfg() -> dict:
    return {"ghost_after_days": 21, "learn_after": 20, "max_lift": 10, **(config.cfg("search").get("outcomes") or {})}


def _e(s) -> str:
    return html.escape(str(s), quote=False)


# ── buttons ────────────────────────────────────────────────────────────────
def keyboard(job: dict, extra: tuple[str, ...] = ()) -> dict | None:
    nxt = NEXT.get(job["status"], FRESH) + extra
    if not nxt:
        return None
    return {"inline_keyboard": [[{"text": LABEL[s], "callback_data": f"o:{s}:{job['id'][:12]}"} for s in nxt]]}


def buttons_for(text: str, db: DB | None = None) -> dict | None:
    """A message with exactly one job card gets that job's outcome buttons; a list of cards gets none."""
    ids = set(CARD_ID.findall(text or ""))
    if len(ids) != 1:
        return None
    j = (db or DB()).get(ids.pop())
    return keyboard(j) if j else None


def tap(db: DB, data: str) -> tuple[str, dict | None]:
    """A button press "o:<outcome>:<job id>" → (what to show, the job's new buttons)."""
    m = re.fullmatch(r"o:([a-z]+):([0-9a-f]{8,12})", data or "")
    if not m or (m[1] not in LABEL):
        return "That button is from an old version: send /check_<id> again.", None
    j = db.get(m[2])
    if not j:
        return "That job is no longer in the database.", None
    what = m[1]
    if what == "wait":                                     # ask again in two weeks
        asked = db.state("ghost_asked", {})
        asked[j["id"]] = str(dt.date.today() + dt.timedelta(days=14))
        db.set_state("ghost_asked", asked)
        return f"OK, I'll ask about {j['company']} again in 2 weeks.", keyboard(j)
    if j["status"] != what:
        db.set_status(j["id"], what, "telegram button")
        if what in DECIDED and db.count("status IN ('interview','offer','rejected','ghosted')") == int(_cfg()["learn_after"]):
            from .rescore import rescore                   # the outcome that turns learning on: re-rank open jobs now
            rescore(db)
    j = db.get(j["id"])
    return f"{LABEL[what]}: {j['title'][:40]} · {j['company'][:30]}", keyboard(j) or {"inline_keyboard": []}


def on_button(api: str, chat: str, cq: dict, post=None) -> None:
    """The bot's callback_query handler: only your chat's buttons count."""
    import requests
    post = post or (lambda method, **kw: requests.post(f"{api}/{method}", json=kw, timeout=20))
    msg = cq.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != str(chat):
        post("answerCallbackQuery", callback_query_id=cq.get("id"), text="Not your chat.")
        return
    db = DB()
    note, markup = tap(db, cq.get("data", ""))
    post("answerCallbackQuery", callback_query_id=cq.get("id"), text=note[:190])
    if markup is not None:
        post("editMessageReplyMarkup", chat_id=chat, message_id=msg.get("message_id"), reply_markup=markup)


# ── no reply: ghosted? ─────────────────────────────────────────────────────
def ghost_prompts(db: DB, today: dt.date | None = None, most: int = 3) -> list[tuple[str, dict]]:
    """Applications still at 'applied' after ghost_after_days, not asked yet (or due again): (message, buttons)."""
    today = today or dt.date.today()
    days = int(_cfg()["ghost_after_days"])
    asked = db.state("ghost_asked", {})
    out = []
    for j in db.query(status=["applied"], limit=100000):
        on = (db.applied_on(j["id"]) or "")[:10]
        if not on or (today - dt.date.fromisoformat(on)).days < days:
            continue
        if j["id"] in asked and asked[j["id"]] > str(today):
            continue
        n = (today - dt.date.fromisoformat(on)).days
        out.append((f"👻 <b>No reply from {_e(j['company'])} in {n} days</b>\n{_e(j['title'])} · applied {on}\n"
                    f"Did they ghost you? · /check_{j['id'][:8]}", keyboard(j, ("wait",))))
        asked[j["id"]] = "9999"                            # asked: not again unless you tap ⏳ Still waiting
        if len(out) >= most:
            break
    db.set_state("ghost_asked", asked)
    return out


# ── the report ─────────────────────────────────────────────────────────────
def role_of(title: str, roles: list[str] | None = None) -> str:
    """The target role (config targets.roles, priority order) a title fits best, or "other"."""
    from .sources.ats import scout
    roles = roles if roles is not None else config.cfg("profile").get("targets", {}).get("roles", [])
    best, pick = 0, "other"
    for r in roles:
        pts = scout().title_points(title, [r])[0]
        if pts > best:
            best, pick = pts, r
    return pick if best >= 15 else "other"


def source_of(j: dict) -> str:
    if j.get("kind") == "govt":
        return "govt"
    srcs = j.get("sources") or []
    srcs = json.loads(srcs) if isinstance(srcs, str) else srcs
    return (srcs[0] if srcs else None) or "?"


def applications(db: DB) -> list[dict]:
    """Every job you applied to: source, role, applied date, outcome (interview/offer/rejected/ghosted/waiting)."""
    ids = {r[0] for r in db.c.execute("SELECT DISTINCT job_id FROM events WHERE kind='applied'")}
    ids |= {r[0] for r in db.c.execute("SELECT id FROM jobs WHERE status IN ('applied','interview','offer','rejected','ghosted')")}
    roles = config.cfg("profile").get("targets", {}).get("roles", [])
    out = []
    for jid in ids:
        j = db.get(jid)
        if not j:
            continue
        out.append({"id": jid, "title": j["title"], "company": j["company"], "source": source_of(j),
                    "role": role_of(j["title"], roles), "on": (db.applied_on(jid) or j.get("status_at") or "")[:10],
                    "outcome": j["status"] if j["status"] in DECIDED else "waiting", "job": j})
    return out


def _line(name: str, rows: list[dict]) -> str:
    n = len(rows)
    c = Counter(r["outcome"] for r in rows)
    ans, won = sum(c[k] for k in ANSWERED), sum(c[k] for k in WON)
    return (f"<b>{_e(name)}</b>: {n} applied · {ans} replied ({ans / n:.0%}) · {won} interview{'s' * (won != 1)} "
            f"({won / n:.0%}) · {c['ghosted']} ghosted · {c['waiting']} waiting")


def report(db: DB, today: dt.date | None = None, weeks: int = 6) -> str:
    today = today or dt.date.today()
    apps = applications(db)
    if not apps:
        return "<b>📈 Outcomes</b>\nNo applications yet. Tap ✅ Applied on a job card, or send /applied_&lt;id&gt;."
    L = [f"<b>📈 Outcomes · {len(apps)} applications</b>",
         "replied = interview, offer or rejection; ghosted and waiting count as no reply"]
    for title, key in (("By source", "source"), ("By role", "role")):
        groups = defaultdict(list)
        for a in apps:
            groups[a[key]].append(a)
        won = lambda rows: sum(r["outcome"] in WON for r in rows)  # noqa: E731
        ranked = sorted(groups.items(), key=lambda kv: (-won(kv[1]) / len(kv[1]), -won(kv[1]), -len(kv[1]), kv[0]))   # interview rate first
        L.append(f"<b>{title}</b>\n" + "\n".join("• " + _line(k, v) for k, v in ranked))
    monday = today - dt.timedelta(days=today.weekday())
    wk = []
    for i in range(weeks - 1, -1, -1):
        start = monday - dt.timedelta(weeks=i)
        rows = [a for a in apps if str(start) <= a["on"] < str(start + dt.timedelta(days=7))]
        if rows:
            wk.append("• " + _line(f"week of {start:%d %b}", rows))
    if wk:
        L.append("<b>By week applied</b>\n" + "\n".join(wk))
    m = model(db)
    L.append(f"<i>Ranking learns from outcomes: on ({m['n']} decided, {m['wins']} with an interview)</i>" if m else
             f"<i>Ranking learns from outcomes after {_cfg()['learn_after']} decided ones "
             f"({sum(a['outcome'] in DECIDED for a in apps)} so far)</i>")
    return "\n\n".join(L)


# ── learn from outcomes ────────────────────────────────────────────────────
STOP = {"and", "the", "for", "with", "intern", "internship", "senior", "junior", "fresher", "graduate", "trainee",
        "engineer", "developer", "job", "role", "team", "india", "bengaluru", "bangalore", "remote", "full", "time"}


def _skills() -> list[str]:
    from .intel import _skills as skills
    return [s.lower() for s in skills()]


def tokens(title: str, description: str = "", skills: list[str] | None = None) -> Counter:
    """What a job "looks like": its title words (×2) and the resume skills its posting names."""
    words = [w for w in re.findall(r"[a-z][a-z0-9+#.]*", title.lower()) if len(w) > 1 and w not in STOP]
    v = Counter({w: 2 for w in words})
    text = f" {description.lower()} "
    for s in skills or []:
        if re.search(rf"(?<![a-z0-9]){re.escape(s)}(?![a-z0-9])", text):
            v[s] += 1
    return v


def _cos(a: Counter, b: dict) -> float:
    dot = sum(a[k] * b.get(k, 0) for k in a)
    na, nb = math.sqrt(sum(x * x for x in a.values())), math.sqrt(sum(x * x for x in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _centroid(vs: list[Counter]) -> dict:
    c: Counter = Counter()
    for v in vs:
        n = math.sqrt(sum(x * x for x in v.values())) or 1
        for k, x in v.items():
            c[k] += x / n
    return {k: x / len(vs) for k, x in c.items()}


def model(db: DB) -> dict | None:
    """None until `learn_after` decided outcomes with at least one interview and one non-interview."""
    rows = [dict(r) for r in db.c.execute(
        "SELECT title, company, description, status FROM jobs WHERE status IN ('interview','offer','rejected','ghosted')")]
    wins = [r for r in rows if r["status"] in WON]
    if len(rows) < int(_cfg()["learn_after"]) or not wins or len(wins) == len(rows):
        return None
    sk = _skills()
    vec = lambda r: tokens(r["title"], r["description"] or "", sk)  # noqa: E731
    won = [(r, vec(r)) for r in wins]
    return {"n": len(rows), "wins": len(wins), "skills": sk, "won": won,
            "win": _centroid([v for _, v in won]), "lose": _centroid([vec(r) for r in rows if r["status"] not in WON])}


def lift(m: dict | None, title: str, description: str = "") -> tuple[int, str | None]:
    """(points to add, reason): how much more this job resembles your interviews than your rejections/ghostings."""
    if not m:
        return 0, None
    v = tokens(title, description, m["skills"])
    gap = _cos(v, m["win"]) - _cos(v, m["lose"])
    pts = max(0, min(int(_cfg()["max_lift"]), round(20 * gap)))
    if not pts:
        return 0, None
    best = max(m["won"], key=lambda rv: _cos(v, rv[1]))[0]
    return pts, f"+{pts}: like jobs that got you interviews ({best['title'][:40]} at {best['company'][:30]})"


def apply(m: dict | None, job) -> None:
    """Add the outcome lift to a scored Job (pipeline.save, rescore). Filtered jobs stay filtered."""
    if job.status == "filtered":
        return
    pts, why = lift(m, job.title, job.description or "")
    if pts:
        job.score = min(100, job.score + pts)
        job.reasons = list(job.reasons) + [why]
