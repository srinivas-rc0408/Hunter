"""What the Telegram bot shows: job lines, paged replies, /today, /stats, /deadlines, /check, company pages, /ref.
Pure renderers (text in, Telegram HTML out); bot.py routes commands to them and re-exports every name here."""
from __future__ import annotations

import datetime as dt
import html
import re
import time

from . import config
from .db import DB
from .notify import apply_link, job_card, pages


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def mobile_apply(db, jid: str) -> str:
    """📱 Everything to apply from the phone: the job card, every apply link, what they ask, and your
    details as tap-to-copy lines. Nothing runs on the laptop."""
    j = db.get(jid) if jid else None
    if not j:
        return "Usage: /check_&lt;id&gt; (the id from a job card)"
    from .notify import job_card
    jid8 = j["id"][:8]
    out = ["📱 <b>Apply from your phone</b>", job_card(j, db)]
    links, seen = [], set()
    for u in [j.get("url"), *(j.get("urls") or [])]:
        if u and u not in seen:
            seen.add(u)
            host = re.sub(r"^https?://(www\.)?", "", u).split("/")[0]
            links.append(f'• <a href="{html.escape(u, quote=True)}">{_e(host)}</a>')
    out.append("<b>Open and apply</b> (tap)\n" + ("\n".join(links[:5]) if links else
               "no link was listed: search the role on the company's careers page"))
    desc = " ".join((j.get("description") or "").split())
    if desc:
        out.append("<b>What they ask</b>\n" + _e(desc[:500]) + ("…" if len(desc) > 500 else ""))
    from .selection import talking_points
    tp = talking_points(j)
    out.append("<b>Your 3 talking points</b> (from your resume)\n" + ("\n".join(f"{i}. {_e(x)}" for i, x in enumerate(tp, 1))
               if tp else "none of your projects' skills appear in this posting"))
    p = config.cfg("profile")
    ans, loc = p.get("answers") or {}, p.get("location") or {}
    links_p = p.get("links") or {}
    ctc = ans.get("expected_ctc_lpa")
    rows = [("Name", p.get("name")), ("Email", p.get("email")), ("Phone", p.get("phone_digits") or p.get("phone")),
            ("City", ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)),
            ("LinkedIn", links_p.get("linkedin") or p.get("linkedin")), ("GitHub", links_p.get("github") or p.get("github")),
            ("Portfolio", links_p.get("portfolio") or p.get("portfolio")),
            ("Notice period", ans.get("earliest_joining") or ans.get("notice_period")),
            ("Expected CTC", f"{ctc} LPA" if ctc else None)]
    copy = "\n".join(f"{k}: <code>{_e(v)}</code>" for k, v in rows if v)
    if copy:
        out.append("<b>Your details</b> (tap a value to copy)\n" + copy)
    out.append(f"Done? ✅ /applied_{jid8} · fill it on the laptop instead: 💻 /laptop_{jid8} · not for you: /skip_{jid8}")
    return "\n\n".join(out)


def _line(j: dict) -> str:
    return (f"{'★ ' if j.get('dream') else ''}{'🏛 ' if j.get('kind') == 'govt' else ''}<b>{j['score']}</b> "
            f"{_e(j['title'][:80])} · {_e(j['company'])}" + (f" · ⏰ {j['deadline']}" if j.get("deadline") else "")
            + f" /show_{j['id'][:8]}" + (f" · {apply_link(j)}" if j.get("url") else ""))


def _paged(head: str, blocks: list[str], cmd: str, page: int) -> str:
    """Page `page` of whole blocks (a job is never cut in half), with the command for the next page."""
    ps = pages("", blocks)
    page = max(1, min(page, len(ps)))
    return (head + (f" · page {page}/{len(ps)}" if len(ps) > 1 else "") + ps[page - 1]
            + (f"\n\nMore: {cmd} {page + 1}" if page < len(ps) else ""))


def _today(db: DB, page: int = 1) -> str:
    """Jobs posted in the last 72 h (posting date, else the day we found it), dream first, govt in its own block."""
    from .util import now, parse_iso
    t = now()
    cut = t - dt.timedelta(hours=72)
    rows = db.c.execute("SELECT * FROM jobs WHERE status != 'skipped' AND score >= 40 AND "
                        "COALESCE(posted_at, found_at) >= ? AND (deadline IS NULL OR deadline >= ?) "
                        "ORDER BY dream DESC, score DESC, found_at DESC", (str(cut.date()), str(t.date()))).fetchall()
    from .db import _row

    def fresh(j):
        p = j.get("posted_at") or j["found_at"]
        if len(p) <= 10:                                   # a date only: counts for its whole day
            return p >= str(cut.date())
        d = parse_iso(p)
        return d is not None and d >= cut
    jobs = [j for j in map(_row, rows) if fresh(j)]
    priv = [j for j in jobs if j["kind"] != "govt"]
    gov = [j for j in jobs if j["kind"] == "govt"]
    if not jobs:
        return "<b>📰 Nothing new in the last 3 days</b>\n/top shows the best open jobs"
    blocks = [job_card(j, db) for j in priv]
    if gov:
        blocks += [f"<b>🏛 Govt ({len(gov)})</b>"] + [job_card(j, db) for j in gov]
    head = f"<b>📰 {len(priv)} new in 3 days · {sum(1 for j in priv if j['dream'])} ★</b>"
    return _paged(head, blocks, "/today", page)


def _stats(db: DB) -> str:
    """Applied today / this week / total, interviews, offers, and how many applications got any answer."""
    today = dt.date.today()
    week = today - dt.timedelta(days=today.weekday())
    q = lambda sql, *a: db.c.execute(sql, a).fetchone()[0]                           # noqa: E731
    applied = "SELECT COUNT(DISTINCT job_id) FROM events WHERE kind='applied'"
    total = q(applied) or db.count("status IN ('applied','interview','offer','rejected','ghosted')")
    ever = lambda k: q("SELECT COUNT(DISTINCT job_id) FROM events WHERE kind=?", k) or db.count("status=?", (k,))  # noqa: E731
    from .tracker import numbers, rows as app_rows
    R = app_rows(db)
    n = numbers(db, R)                               # real answers only: viewed, test, interview, rejection, offer
    total = max(total, n["Applied"])
    rate = (f"{n['Response rate']} ({n['answered']} of {n['Applied']} got a real answer: viewed, test, interview, "
            f"rejection or offer; automatic confirmations don't count) · interview rate {n['Interview rate']}"
            if n["Applied"] else "no applications yet")
    from .selection import version_lines
    ver = version_lines(R)
    opened = db.count("kind='private' AND status IN ('new','alerted','queued') AND score >= 40")
    govt_open = db.count("kind='govt' AND status IN ('new','alerted') AND (deadline IS NULL OR deadline >= ?)", (str(today),))
    return (f"<b>📊 Your numbers</b>\n"
            f"Applied: today {q(applied + ' AND at >= ?', str(today))} · this week {q(applied + ' AND at >= ?', str(week))} · total {total}\n"
            f"Interviews {ever('interview')} · Offers {ever('offer')} · Rejected {ever('rejected')}\n"
            f"Response rate: {rate}\n"
            + (f"{_e(ver)}\n" if ver else "") +
            f"Open matches {opened} · tracked {db.count()} · waiting for you {db.count('status=?', ('needs_you',))} · govt open {govt_open}")


def _deadlines(db: DB, days: int = 14, page: int = 1) -> str:
    """Every last date in the next `days` days, govt and private, soonest first."""
    today = dt.date.today()
    rows = db.c.execute("SELECT * FROM jobs WHERE deadline IS NOT NULL AND deadline >= ? AND deadline <= ? AND "
                        "status NOT IN ('skipped','applied','rejected','ghosted') AND (kind = 'govt' OR score >= 40) "
                        "ORDER BY deadline, score DESC", (str(today), str(today + dt.timedelta(days=days)))).fetchall()
    from .db import _row
    jobs = [j for j in map(_row, rows) if "eligibility:ineligible" not in (j.get("flags") or [])]
    if not jobs:
        return f"<b>🗓 Deadlines, next {days} days</b>\nnone"
    lines = []
    for j in jobs:
        n = (dt.date.fromisoformat(j["deadline"][:10]) - today).days
        when = "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"
        lines.append(f"{'🔴' if n <= 3 else '🟡' if n <= 7 else '⚪'} <b>{dt.date.fromisoformat(j['deadline'][:10]):%d %b}</b> ({when}) · "
                     f"{'🏛 ' if j['kind'] == 'govt' else ''}{_e(j['title'][:70])} · {_e(j['company'])}"
                     + (f" · {apply_link(j)}" if j.get("url") else "") + f" /show_{j['id'][:8]}")
    return _paged(f"<b>🗓 Deadlines, next {days} days: {len(jobs)}</b>", lines, "/deadlines", page)


_PICK: dict = {}                                        # numbered company choices waiting for "2"
_COMPANY_PAGES: dict[str, tuple[float, str, list[str]]] = {}   # company key → (when, header, cards): pages 2, 3 …


def _clock_ist() -> str:
    from .util import now
    t = now()
    return f"{t:%I:%M}".lstrip("0") + (" am" if t.hour < 12 else " pm") + " IST"


def _choices(q: str, online: bool = True) -> list[dict]:
    """Companies whose name holds every word typed (so 'Chairperson of …' and unrelated Wikidata hits drop out)."""
    from . import companies
    from .util import norm
    words = norm(q).split()
    return [x for x in companies.suggest(q, online=online) if x["source"] != "as typed"
            and all(w in norm(x["name"]).split() or any(t.startswith(w) for t in norm(x["name"]).split()) for w in words)]


def _company_cmd(arg: str, db: DB, watch: bool = False) -> str | list[str]:
    """/company <name> (page 2: /company <name> 2) · /watch <name>. Several matches → numbered choices."""
    from . import companies
    from .util import norm
    page = 1
    m = re.match(r"^(.*\S)\s+(\d{1,2})$", arg)
    if m and companies.cname(m.group(1)) in _COMPANY_PAGES:
        arg, page = m.group(1), int(m.group(2))
    hit = _COMPANY_PAGES.get(companies.cname(arg))
    if page > 1 and hit and time.time() - hit[0] < 1800:
        return _paged(hit[1], hit[2], f"/company {_e(arg)}", page)
    cands = _choices(arg)
    exact = [c for c in cands if norm(c["name"]) == norm(arg)]
    if exact or len(cands) <= 1:
        pick = (exact or cands or [{"name": arg, "source": "as typed"}])[0]
    else:
        _PICK.update(at=time.time(), cmd="watch" if watch else "company", items=cands[:8])
        return (f"<b>Which {_e(arg)}?</b>\n" + "\n".join(f"{i}. {_e(c['name'])} <i>({_e(c.get('hint') or c['source'])})</i>"
                                                        for i, c in enumerate(cands[:8], 1)) + "\nReply with the number.")
    return _company_report(pick, db, watch)


def _fact(label: str, value, src: str | None) -> str:
    """A company fact with the link it came from, or "not found": nothing about a company is shown unsourced."""
    if value in (None, "") or not src:
        return f"{label}: not found"
    return f"{label}: {_e(value)} <i>(<a href=\"{html.escape(src, quote=True)}\">source</a>)</i>"


def _company_report(pick: dict, db: DB, watch: bool = False) -> str | list[str]:
    from . import companies, dreams
    from .score import score
    from .util import parse_iso
    name = pick["name"]
    if watch:
        info = companies.resolve(name, pick.get("domain"), pick.get("wikidata"))
        return (f"✓ watching {_e(info['name'])}: every scan includes it" if companies.watch(info)
                else f"{_e(info['name'])}: already watched, or no platform/careers page found")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:                 # news + about run while the boards are searched
        f_news = ex.submit(companies.news, name, 90, 6)
        info, rows, errors = companies.live(name, pick.get("domain"), pick.get("wikidata"))
        f_about = ex.submit(companies.about, info)
        res = companies.listings(info, rows=rows)
        today = str(dt.date.today())
        cards, fit = [], 0
        rows = sorted(res["listings"], key=lambda r: r.get("posted") or "", reverse=True)       # newest first …
        for r in sorted(rows, key=lambda r: (not r["fit"], -r["score"])):                     # … fit and best on top
            if (r.get("deadline") or "9999") < today or r.get("status") in ("expired", "rejected"):
                continue
            j = db.get(r["id"])
            if not j:                                             # new to us: store it (scored) so /check works
                db.upsert(score(companies.to_job(r)))
                j = db.get(r["id"])
            if not j:
                continue
            fit += r["fit"]
            cards.append(job_card(j, db) + ("" if r["fit"] else f"\n<i>outside your filters: {_e(r['why'][:90])}</i>"))
        try:
            news = f_news.result(timeout=10)
        except Exception:                                 # news down must not hide what it does (and vice versa)
            news = []
        try:
            ab = f_about.result(timeout=10)
        except Exception:
            ab = {}
    when = _clock_ist()
    missed = [e for e in errors if "not included" in e or "still" in e]
    head = (f"<b>🏢 {_e(info['name'])}</b> · {len(cards)} open roles · checked {when}"
            + (f"\n{fit} fit your filters" if cards else "")
            + (f"\n<i>{_e('; '.join(missed))}</i>" if missed else ""))
    if not cards:
        main = (f"<b>🏢 {_e(info['name'])}</b> · 0 open roles · checked {when}\n"
                f"Nothing open on its careers site, LinkedIn, Naukri or Indeed right now."
                + (f"\n<i>{_e('; '.join(missed))}</i>" if missed else "")
                + f"\nGet it in every scan: <code>/watch {_e(info['name'])}</code>")
    else:
        _COMPANY_PAGES[companies.cname(name)] = (time.time(), head, cards)
        main = _paged(head, cards, f"/company {_e(name)}", 1)
    # second message: news, what it does, Bengaluru office, your applications, dream status
    blr = [r for r in res["listings"] if re.search(r"bengaluru|bangalore", r.get("location") or "", re.I)]
    blr_src = next((r.get("apply_url") or r.get("url") for r in blr if r.get("apply_url") or r.get("url")), None)
    mine = [j for j in db.query(status=["applied", "interview", "offer", "rejected"], limit=100000)
            if companies.same_company(j["company"], info["name"])]
    apps = [f"• {_e(j['title'][:60])} · <b>{j['status']}</b>" + (f" · applied {parse_iso(on):%d %b}" if (on := db.applied_on(j['id'])) else "")
            for j in mine[:8]]
    size, src = ab.get("employees"), ab.get("source")
    lines = [f"<b>ℹ️ {_e(info['name'])}</b>",
             _fact("What it does", ab.get("description"), src),
             _fact("Size", f"{int(float(size)):,} employees" if size and str(size).replace(".", "").isdigit() else None, src),
             _fact("Founded", ab.get("founded"), src),
             _fact("Bengaluru office", f"yes, {len(blr)} open role(s) there" if blr else None, blr_src),
             f"Dream: {'★ yes' if dreams.find(info['name']) else 'no · <code>/dream ' + _e(info['name']) + '</code>'}",
             "<b>Your applications</b>", *(apps or ["none yet"]),
             f"Watch it in every scan: <code>/watch {_e(info['name'])}</code>",
             "<b>News, last 90 days</b>"]
    lines += [f"• {n['date'][8:]} {dt.date.fromisoformat(n['date']):%b} · <a href=\"{html.escape(n['url'], quote=True)}\">"
              f"{_e(n['title'][:110])}</a> <i>({_e(n['source'])})</i>" for n in news] or ["none found"]
    for label, site in (("Careers", info.get("careers_url")), ("Website", not info.get("careers_url") and info.get("website"))):
        if site:
            lines.append(f"{label}: <a href=\"{html.escape(site, quote=True)}\">{_e(site[:60])}</a>")
    return [main, "\n".join(lines)]


def _ref(db: DB, inline_id: str | None, arg: str) -> str:
    """ref <id> <name> · ref done <id> · ref (list). The name is only what you typed: nothing is looked up."""
    from . import tracker
    words = arg.split()
    if not inline_id and not words:
        rs = tracker.referrals(db)
        if not rs:
            return "<b>🤝 Referrals</b>\nnone yet. <code>ref ab12cd34 Priya Sharma</code> when you ask someone"
        return "<b>🤝 Referrals</b>\n" + "\n".join(
            f"{'✅' if r['status'] == 'referred' else '·'} {_e(r['at'][:10])} · {_e(r['name'])} · "
            f"{_e((r['job'] or {}).get('company', 'job deleted'))} · {_e(r['status'])} /show_{r['job_id'][:8]}" for r in rs[:40])
    done = (words[:1] == ["done"])
    if done:
        words = words[1:]
    jid = inline_id or (words.pop(0) if words else "")
    j = db.get(jid) if re.fullmatch(r"[0-9a-f]{6,16}", jid or "") else None
    if not j:
        return f"✗ no job {_e(jid)}. Usage: <code>ref ab12cd34 Priya Sharma</code> · <code>ref done ab12cd34</code>"
    try:
        if done:
            r = tracker.refer_done(db, j["id"])
        elif not words:
            return "Usage: <code>ref ab12cd34 Priya Sharma</code> (who you asked for the referral)"
        else:
            tracker.refer(db, j["id"], " ".join(words))
            r = tracker.referral(db, j["id"])
    except ValueError as exc:
        return f"✗ {_e(exc)}"
    tracker.refresh(db)
    return (f"🤝 {_e(j['title'][:60])} · {_e(j['company'])}: <b>{_e(r['name'])}</b> → {_e(r['status'])}"
            + ("" if done else f"\nWhen they refer you: <code>ref done {j['id'][:8]}</code>"))


def offline_note(since: float, secs: float) -> str | None:
    """One alert when the bot is back after >= 2 h without Telegram. secs is monotonic time, which stands still
    while the laptop sleeps, so only time the laptop was on (and offline) counts."""
    if secs < 2 * 3600:
        return None
    return (f"⚠️ <b>bot offline since {time.strftime('%d %b %H:%M', time.localtime(since))}</b> "
            f"({secs / 3600:.1f} h with the laptop on but Telegram unreachable). Back now; "
            f"anything you sent meanwhile is answered next.")
