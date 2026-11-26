"""Telegram command bot: triage jobs from your phone (`hunt bot`, runs as a service).

Only answers YOUR chat (TELEGRAM_CHAT_ID); everyone else is ignored.
Commands (also tappable from alerts as /show_<id>, /skip_<id>):
  /brief            the morning brief now
  /top [n]          best open jobs (default 10)
  /govt             govt notices with deadlines
  /review           forms waiting for your approval
  /stats            pipeline numbers
  /show <id>        one job: why it matched, link, eligibility
  /skip <id>        hide it          /queue <id>   put it at the front of the apply queue
  /applied <id>     /interview <id>  /rejected <id>  /offer <id>   update status
  /scan             start a scan on the laptop now
  /company <name>   that company's open listings (Karnataka first) with links
  /watch <name>     include that company in every scan
  /questions        what forms asked that isn't in your files
  /answer <n> <text>  answer question n (saved as your fact; waiting jobs re-queue)
  /help
Nothing here submits an application: applying stays in `hunt apply` / the Review Center.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import subprocess
import sys
import time

import requests

from . import config
from .config import env
from .notify import apply_link, job_card, pages
from .db import DB

STATUS_CMDS = {"skip": "skipped", "applied": "applied", "interview": "interview", "rejected": "rejected",
               "offer": "offer", "queue": "queued"}


KNOWN = {"start", "help", "menu", "brief", "top", "today", "new", "govt", "deadlines", "eligible", "search", "health", "review", "stats", "show", "scan", "company", "watch", "intel",
         "questions", "answer", "ping", "form", "fix", "approve", "check", "laptop", "dream", "undream", "dreams", "resume", "mail", "sheet",
         "ref", *STATUS_CMDS}

HELP = """<b>🎯 JobHunter</b> · <b>just type what you want</b>: <i>find ML intern jobs in Bengaluru posted this week</i>,
<i>what does the Birlasoft job need that I don't have?</i>, <i>mark ab12cd34 as interview</i> (changes ask you for a yes first)
Commands work too, with or without the slash: <i>top 5</i>, <i>govt karnataka</i>, or just a company name

<b>Jobs</b>
/today · posted in the last 3 days · <code>today 2</code> next page
/top · best open jobs · <code>top 5</code>
/search · any open job by role, skill or company · <code>search python intern</code>
/company · one company: every open role, news, your applications · <code>company Zoho</code>
/intel · what a company hires for, from our scans + 90 days of news · <code>intel Birlasoft</code>
Paste any company or job link (or <i>tell me about Zoho</i>) · a full profile, every fact with its source
/deadlines · every last date in the next 14 days
/brief · the morning brief now

<b>Govt</b>
/govt · open notices, eligible first · <code>govt karnataka</code>, <code>govt central</code>, <code>govt all</code>
<code>govt isro</code>, <code>govt kpsc</code>, <code>govt engineer</code> · search govt notices only
/eligible · posts you can apply to now

<b>One job</b> (tap the links on a card)
/show_&lt;id&gt; · why it matched · /form_&lt;id&gt; · every filled field
/fix_&lt;id&gt; · change a field · <code>/fix_ab12cd34 3 15 days</code>
📱 /check_&lt;id&gt; · apply from your phone: the apply link + your details to copy
✅ /applied_&lt;id&gt; · tell me you applied (from the phone or anywhere)
💻 /laptop_&lt;id&gt; · open it in the bot browser on the laptop · /approve_&lt;id&gt; · submit as shown
/skip_&lt;id&gt; · /queue_&lt;id&gt; · /applied_&lt;id&gt; · /interview_&lt;id&gt; · /offer_&lt;id&gt;

<b>You</b>
/stats · applied today, this week, total, interviews, offers, response rate
/applied · every application with its date, application ID and form link
/mail · application emails sorted: offers, interviews, tests, rejections · <code>mail 30</code>
/sheet · the Excel tracker (every application + email replies), updated now
   🤝 <code>ref ab12cd34 Priya Sharma</code> · you asked Priya for a referral · <code>ref done ab12cd34</code> · she referred you · /ref · all of them
   <i>add a column Referral</i> · <i>put 'asked Rahul' in Referral for ab12cd34</i> · <i>remove ab12cd34 from the sheet</i> · <i>show hidden</i>
💰 <i>how much would a fresher AI engineer get at Zoho?</i> · an estimate with sources and confidence, never the posting's pay
/review · forms waiting for you · /questions · then <code>answer 1 Yes</code>
/dream · <code>dream Zoho</code> (★ +15, watched) · /dreams · <code>undream Zoho</code>
/watch · add a company to every scan · <code>watch Bosch</code>
/resume · the current resume (send a PDF to replace it)

<b>System</b>
/health · every check, ✓/✗ with the exact fix · /scan · scan now · /ping · alive?"""


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
    total = q(applied) or db.count("status IN ('applied','interview','offer','rejected')")
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
                        "status NOT IN ('skipped','applied','rejected') AND (kind = 'govt' OR score >= 40) "
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


ABOUT = re.compile(r"^(?:tell me about|about|profile of|research) (?:the company )?(.{2,60}?)\W*$", re.I)
_LINKS: dict[str, tuple[float, list[str]]] = {}         # company key → (when, messages): a pasted link is cached 24 h


def _link_report(t: str, db: DB, progress=None) -> str | list[str]:
    """A pasted link or "tell me about <company>": our DB first (fast), then the live profile, every fact sourced."""
    from . import companies, intel, linkinfo
    m = ABOUT.match(t)
    ident = ({"name": m.group(1), "read": True, "why": "", "facts": [], "domain": None} if m
             else linkinfo.identify(t, companies._get, db))
    if not ident["name"]:
        return f"⚠ {_e(ident['why'])}.\nSend the company name instead: <i>tell me about &lt;company&gt;</i>"
    name, key = ident["name"], companies.cname(ident["name"])
    hit = _LINKS.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    hist = intel.rows(db, name)
    mine = [j for j in hist if j["status"] in ("applied", "interview", "offer", "rejected")]
    known = sorted((j for j in hist if j["status"] in ("new", "alerted", "queued", "needs_you", "filled")),
                   key=lambda j: -j["score"])
    fast = (f"<b>🏢 {_e(name)}</b> · from our scans: {len(hist)} postings seen, {len(known)} open, {len(mine)} applied"
            + (f"\n<i>{_e(ident['why'])}: nothing on this profile is filled in from memory.</i>" if not ident["read"] else "")
            + "".join(f"\n• {_e(j['title'][:60])} · {j['score']} · /check_{j['id']}" for j in known[:5])
            + "\n<i>Checking its careers site and news now…</i>")
    if progress:
        progress(fast)
    msgs = _company_report({"name": name, "domain": ident.get("domain")}, db)
    msgs = [msgs] if isinstance(msgs, str) else list(msgs)
    live_open = [j for j in intel.rows(db, name) if j["status"] in ("new", "alerted", "queued", "needs_you", "filled")]
    a = intel.analyse(live_open, intel._skills())                      # its open roles, incl. the ones just fetched
    L = [f"<b>🔍 {_e(name)}: what it hires for vs your resume</b> <i>({a['n']} open postings)</i>"]
    top = a["skills"][:8]
    if top:
        have = {x.lower() for x in intel._skills()}
        hit_n = sum(s.lower() in have for s, _ in top)
        L.append(f"Match: {hit_n} of its top {len(top)} skills ({hit_n / len(top):.0%}): "
                 + ", ".join(("✓ " if s.lower() in have else "✗ ") + f"{_e(s)} {v:.0%}" for s, v in top))
        gaps = [s for s, _ in top if s.lower() not in have][:3]
        L.append("Top gaps: " + (", ".join(_e(g) for g in gaps) if gaps else "none"))
    else:
        L.append("No open postings with a description to compare yet")
    for label, value, src in ident["facts"]:
        L.append(_fact(_e(label), value, src))
    try:
        news = companies.news(name, 90, 8)
    except Exception:
        news = []
    sig = intel.signals(news)
    L.append("<b>Hiring signals, 90 days</b>\n" + ("\n".join(f"• {k}: {n['date']} <a href=\"{html.escape(n['url'], quote=True)}\">"
                                                             f"{_e(n['title'][:100])}</a>" for k, n in sig[:5]) or "none in the news"))
    L.append(f"<code>/dream {_e(name)}</code> · <code>/watch {_e(name)}</code> · <code>/intel {_e(name)}</code>")
    out = ([] if progress else [fast]) + msgs + ["\n".join(L)]
    _LINKS[key] = (time.time(), out)
    return out


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


TEXT_ARGS = {"ref", "company", "watch", "intel", "govt", "eligible", "dream", "undream", "answer", "fix", "mail", "search"}


def _is_command(t: str) -> bool:
    """'top 5', 'skip ab12cd34', 'company Zoho' are commands; 'top 5 GenAI roles at product companies' is a question."""
    word, _, rest = t.partition(" ")
    w, rest = word.lower(), rest.strip()
    if w not in KNOWN:
        return False
    if not rest or re.fullmatch(r"[0-9a-f]{6,16}|\d{1,3}(?:\s+\d{1,3})?", rest):
        return True
    return w in TEXT_ARGS and "?" not in rest and len(rest.split()) <= {"govt": 2, "eligible": 2, "ref": 8}.get(w, 4)


def handle(text: str, db: DB | None = None, chat: str = "", progress=None) -> str | list[str]:
    """Pure command handler (unit-tested): text in, HTML reply out (a list = several whole messages).
    Anything that isn't a command goes to the chat assistant (jobhunter/assistant.py)."""
    db = db or DB()
    t = (text or "").strip()
    if t.isdigit() and _PICK.get("items") and time.time() - _PICK.get("at", 0) < 900:
        items = _PICK["items"]
        if not 1 <= int(t) <= len(items):
            return f"Pick 1 to {len(items)}."
        return _company_report(items[int(t) - 1], db, watch=_PICK.get("cmd") == "watch")
    from . import assistant
    if t and not t.startswith("/") and (assistant.YES.match(t) or assistant.NO.match(t) or assistant.MORE.match(t)):
        return assistant.reply(t, chat, db, progress)  # "yes" confirms a change: never a company called Yes…
    from . import linkinfo
    if t and not t.startswith("/") and (linkinfo.is_url(t) or ABOUT.match(t)):
        return _link_report(t, db, progress)            # a pasted link, or "tell me about Zoho"
    if t and not t.startswith("/") and _is_command(t):
        t = "/" + t                                    # "help", "top 5", "stats" work without the slash
    elif t and not t.startswith("/") and len(t.split()) <= 3 and len(t) >= 3 and _choices(t, online=False):
        return _company_cmd(t, db)                     # just a company name: "Zoho", "aditya birla"
    elif t and not t.startswith("/"):
        return assistant.reply(t, chat, db, progress)  # "find ML intern jobs in Bengaluru posted this week"
    m = re.match(r"^/(\w+?)(?:_([0-9a-f]{6,16}))?(?:@\w+)?(?:\s+(.*))?$", t, re.S)
    if not m:
        return "Send /help (or just <i>help</i>) for commands."
    cmd, inline_id, arg = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
    jid = inline_id or (arg.split()[0] if arg else "")
    if cmd in ("start", "help", "menu"):
        return HELP
    if cmd in ("dream", "undream", "dreams"):
        from . import dreams
        if cmd == "dreams" or not arg:
            ds = dreams.names()
            return ("<b>★ Dream companies</b>\n" + ("\n".join("• " + _e(d) for d in ds) or "none yet") +
                    "\nAdd: <code>/dream Company</code> · remove: <code>/undream Company</code>")
        ok, msg = (dreams.add if cmd == "dream" else dreams.remove)(arg)
        if not ok:
            return "· " + _e(msg)
        notes = dreams.after_change(arg, cmd == "dream")
        return "✓ " + _e(msg) + "\n" + "\n".join("• " + _e(n) for n in notes)
    if cmd == "form":
        from .apply.fixes import latest
        from .notify import form_lines
        j, app = latest(db, jid)
        if not j:
            return "Usage: /form_&lt;id&gt;"
        if not app:
            return f"{_e(j['title'])} · {_e(j['company'])}: not filled yet. /check_{j['id'][:8]} fills it on the laptop."
        meta = db.app_meta(j["id"])
        form_url = meta["form_url"] or app.get("page_url") or ""
        head = (f"<b>{_e(j['title'])}</b> · {_e(j['company'])}\n"
                f"{'preview' if app.get('dry') else app.get('outcome')} · {_e(app.get('site'))} · {_e((app.get('at') or '')[:16])}\n"
                + (f"Application ID: <code>{_e(meta['conf_id'])}</code>\n" if meta["conf_id"] else "")
                + (f"<a href=\"{html.escape(form_url, quote=True)}\">Open form</a>\n" if form_url else "") +
                f"<i>{_e((app.get('why') or '')[:200])}</i>\n")
        body = "\n".join(form_lines(app.get("fields") or [])) or "No form fields were recorded."
        return head + body + (f"\n\nChange one: <code>/fix_{j['id'][:8]} 3 new value</code>\n"
                              f"Send as shown: /approve_{j['id'][:8]} · read it on the laptop: /check_{j['id'][:8]}")
    if cmd == "fix":
        from .apply.fixes import fix
        rest = arg.split(None, 1) if inline_id else (arg.split(None, 1)[1].split(None, 1) if len(arg.split(None, 1)) > 1 else [])
        if not jid or len(rest) < 2 or not rest[0].isdigit():
            return "Usage: <code>/fix_&lt;id&gt; 3 your answer</code> (the number from /form)"
        ok, msg = fix(db, jid, int(rest[0]), rest[1])
        return ("✓ " if ok else "✗ ") + _e(msg)
    if cmd == "check":
        return mobile_apply(db, jid)
    if cmd in ("approve", "laptop"):
        j = db.get(jid) if jid else None
        if not j:
            return f"Usage: /{cmd}_&lt;id&gt;"
        import os
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return ("The bot service can't see the laptop screen, so it can't open the browser. On the laptop run "
                    "<code>scripts/install-services.sh</code> once from your desktop session.")
        args = ["apply", j["id"], "--submit" if cmd == "approve" else "--approve"]
        # the laptop runs it in the background: say so plainly, and how to apply from the phone instead
        subprocess.Popen([sys.executable, "-m", "jobhunter.cli", *args], cwd=str(config.home()),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, "JH_NOTIFY": "1"})
        return (f"⏳ Sending <b>{_e(j['title'])}</b> · {_e(j['company'])} from the laptop with the values in /form. "
                "You'll get ✅ Applied, or ✋ if the site asks something new." if cmd == "approve" else
                f"⏳ Opening <b>{_e(j['title'])}</b> on the laptop's bot browser. Read the form there and press Submit "
                f"on the panel. Not at the laptop? /check_{j['id'][:8]} gives you the link to apply from your phone.")
    if cmd == "ref":
        return _ref(db, inline_id, arg)
    if cmd == "resume":
        return "__RESUME__"
    if cmd == "sheet":
        return "__SHEET__"
    if cmd == "mail":
        from .mail import summary_text
        days = int(arg) if arg.strip().isdigit() else 7
        return summary_text(db, min(days, 90))
    if cmd == "ping":
        return f"🟢 alive · {time.strftime('%d %b %H:%M')} · {db.count('1=1')} jobs tracked"
    if cmd == "questions":
        from .apply.questions import pending
        rows = pending()
        if not rows:
            return "No open questions. ✅"
        lines = [f"{i}. {_e(r['label'][:90])} ({len(r['jobs'])} job(s))" +
                 (f"\n   options: {_e(' | '.join(r['options'][:8]))}" if r.get("options") else "") for i, r in enumerate(rows[:15], 1)]
        return "<b>❓ Questions</b> (answer with true facts only)\n" + "\n".join(lines) + "\n\nReply: /answer 1 Yes"
    if cmd == "answer":
        from .apply.questions import answer
        n, _, val = arg.partition(" ")
        if not n.isdigit() or not val.strip():
            return "Usage: /answer &lt;number&gt; &lt;your answer&gt;"
        try:
            res = answer(int(n), val)
        except (KeyError, ValueError) as exc:
            return f"✗ {_e(exc)}"
        return (f"✓ saved: {_e(res['label'][:70])} → <b>{_e(res['value'])}</b>" +
                (f"\n{len(res['requeued'])} job(s) back in the apply queue" if res["requeued"] else ""))
    if cmd == "intel":
        from . import intel
        return intel.report(db, arg) if arg else "Usage: <code>/intel Birlasoft</code>"
    if cmd in ("company", "watch"):
        if not arg:
            return f"Usage: <code>/{cmd} Zoho</code>"
        return _company_cmd(arg, db, watch=cmd == "watch")
    if cmd == "brief":
        from .morning import as_telegram_pages, build
        return as_telegram_pages(build(db=db, heartbeat=False))
    if cmd == "top":
        nums = [int(x) for x in arg.split() if x.isdigit()]
        n, page = (nums + [1])[:2] if nums else (10, 1)       # /top · /top 5 · /top 10 2 (page 2)
        n = max(1, min(n, 50))
        jobs = db.query(status=["new", "alerted", "queued"], kind="private", min_score=40, limit=n * 2)
        today = str(dt.date.today())
        jobs = [j for j in jobs if not j.get("deadline") or j["deadline"] >= today]     # last date passed: gone
        jobs.sort(key=lambda j: (j["status"] != "queued", not j["dream"], -j["score"]))
        if not jobs:
            return "<b>Top open jobs</b>\nnone: run /scan"
        return _paged(f"<b>Top {min(n, len(jobs))} open jobs</b>", [job_card(j, db) for j in jobs[:n]], f"/top {n}", page)
    if cmd in ("today", "new"):
        return _today(db, int(arg) if arg.isdigit() else 1)
    if cmd in ("govt", "eligible"):
        from .notify import govt_card, govt_open
        words = arg.lower().split()
        scope = next((w for w in words if w in ("all", "karnataka", "central")), "all")
        page = int(next((w for w in words if w.isdigit()), 1))
        jobs = govt_open(db.query(kind="govt", limit=1000), scope=scope, eligible_only=cmd == "eligible")
        find = [w for w in words if w not in ("all", "karnataka", "central") and not w.isdigit()]
        if find:                                        # "govt isro", "govt kpsc", "govt engineer": govt notices only
            hay = lambda j: " ".join([j["title"], j["company"], " ".join(j.get("sources") or []),  # noqa: E731
                                      (j.get("details") or {}).get("post") or "", j.get("description") or ""]).lower()
            jobs = [j for j in jobs if all(w in hay(j) for w in find)]
        if "all" not in words:                          # posts you can't apply to: only in /govt all
            jobs = [j for j in jobs if "eligibility:ineligible" not in (j.get("flags") or [])]
        title = ("✅ Govt posts you're eligible for now" if cmd == "eligible" else
                 {"karnataka": "🏛 Karnataka govt notices", "central": "🏛 Central govt / PSU notices"}.get(scope, "🏛 Govt notices"))
        if find:
            title += f" · “{_e(' '.join(find))}”"
        from .notify import registration_text, registrations
        regs = "🪪 " + " · ".join(registration_text(r) for r in registrations()) if registrations() else ""
        if not jobs:
            return (f"<b>{title}</b>\nnone open right now" + ("\n/govt all shows every open notice" if cmd == "eligible" else "")
                    + (f"\n\n{regs}" if regs else ""))
        today = dt.date.today()
        soon = sum(1 for j in jobs if j.get("deadline") and 0 <= (dt.date.fromisoformat(j["deadline"][:10]) - today).days <= 3)
        prof = config.cfg("profile")
        head = f"<b>{title}: {len(jobs)} open</b>" + (f"\n{regs}" if regs else "") + (f"\n🔴 <b>{soon} close{'s' if soon == 1 else ''} in 3 days or less</b>" if soon else "")
        more = f"/{cmd}" + (f" {scope}" if scope != "all" or "all" in words else "") + (f" {' '.join(find)}" if find else "")
        return _paged(head, [govt_card(j, prof, today) for j in jobs], more, page)
    if cmd == "deadlines":
        return _deadlines(db, page=int(arg) if arg.isdigit() else 1)
    if cmd == "review":
        jobs = db.query(status=["needs_you"], limit=20)
        return (f"<b>Waiting for approval: {len(jobs)}</b>\n" + "\n".join(_line(j) for j in jobs)
                + "\nApprove them at localhost:8765 on your laptop.") if jobs else "Nothing waiting. ✅"
    if cmd == "stats":
        return _stats(db)
    if cmd == "health":
        from . import health
        return health.as_telegram(health.checks(live=False))
    if cmd == "search":
        if not arg:
            return "Usage: <code>/search python intern</code> (role, skill or company)"
        from . import assistant
        assistant.ensure(db)
        return assistant._finish(db, {"chat": chat or str(env("TELEGRAM_CHAT_ID") or "me"),
                                      "progress": progress or (lambda s: None)}, t, "search_jobs", {"query": arg}, "")
        words = [w for w in re.split(r"\s+", arg.lower()) if w and not w.isdigit()]
        page = int(next((w for w in arg.split() if w.isdigit()), 1))
        sql = ("SELECT * FROM jobs WHERE status IN ('new','alerted','queued','needs_you') AND (deadline IS NULL OR deadline >= ?)"
               + "".join(" AND LOWER(title || ' ' || company || ' ' || COALESCE(description, '')) LIKE ?" for _ in words)
               + " ORDER BY (LOWER(title || ' ' || company) LIKE ?) DESC, dream DESC, score DESC LIMIT 60")
        from .db import _row
        rows = [_row(r) for r in db.c.execute(sql, (str(dt.date.today()), *[f"%{w}%" for w in words], f"%{words[0]}%"))]
        if not rows:
            return f"<b>🔎 {_e(arg)}</b>\nno open jobs match. Try fewer words, or <code>company {_e(arg)}</code> for a live check."
        from .notify import govt_card
        cards = [govt_card(j) if j["kind"] == "govt" else job_card(j, db) for j in rows]
        return _paged(f"<b>🔎 {_e(' '.join(words))}: {len(rows)}{'+' if len(rows) == 60 else ''} open</b>", cards,
                      f"/search {' '.join(words)}", page)
    if cmd == "show":
        j = db.get(jid) if jid else None
        if not j:
            return "Usage: /show &lt;id&gt;"
        rs = "\n".join("• " + _e(r[:140]) for r in (j.get("reasons") or [])[:8])
        return (f"{_line(j)}\n{_e(j.get('location'))} · status <b>{_e(j['status'])}</b>\n{rs}\n"
                f"<a href=\"{html.escape(j['url'] or '', quote=True)}\">Open posting</a>\n/skip_{j['id'][:8]} · /queue_{j['id'][:8]}")
    if cmd == "applied" and not jid:                   # bare "applied": your applications, newest first
        rows = db.query(status=["applied", "interview", "offer", "rejected"], limit=100000)
        rows = sorted(((db.applied_on(j["id"]) or j.get("status_at") or "", j) for j in rows), key=lambda x: x[0], reverse=True)
        if not rows:
            return "<b>Your applications</b>\nnone yet. Mark one: <code>/applied_&lt;id&gt;</code>"
        mark = {"interview": "🎤", "offer": "🎉", "rejected": "✗", "applied": "·"}
        def extra(j):
            m = db.app_meta(j["id"])
            return ((f" · ID <code>{_e(m['conf_id'])}</code>" if m["conf_id"] else "")
                    + (f" · <a href=\"{html.escape(m['form_url'], quote=True)}\">form</a>" if m["form_url"] else ""))
        lines = [f"{mark[j['status']]} {_e(on[:10])} · {_e(j['title'][:60])} · {_e(j['company'])}{extra(j)} /show_{j['id'][:8]}"
                 for on, j in rows]
        page = int(arg) if arg.isdigit() else 1
        return _paged(f"<b>Your applications: {len(rows)}</b>", lines, "/applied", page)
    if cmd in STATUS_CMDS:
        j = db.get(jid) if jid else None
        if not j:
            return f"Usage: /{cmd} &lt;id&gt;"
        db.set_status(j["id"], STATUS_CMDS[cmd], "telegram")
        from . import urgent
        urgent.tick(db)                                 # applied / skipped: its 🚨 pin comes off now
        from .tracker import refresh
        refresh(db)
        done = f"✓ {_e(j['title'][:60])} · {_e(j['company'])} → <b>{STATUS_CMDS[cmd]}</b>"
        if cmd == "interview":                          # one-screen prep pack, facts from resume.yaml only
            from .prep import pack
            return [done, pack(j)]
        return done
    if cmd == "scan":
        subprocess.Popen([sys.executable, "-m", "jobhunter.cli", "scan"], cwd=str(config.home()),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return "Scan started on the laptop. New matches will arrive as alerts."
    return f"Unknown command /{_e(cmd)}. Send /help"


SLOW = {"search", "today", "brief", "company", "watch", "intel", "scan", "top", "govt", "dream", "undream", "approve", "check", "interview"}


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)          # → journalctl --user -u jobhunter-bot


def send(api: str, chat: str, text: str) -> bool:
    """HTML first; if Telegram rejects the HTML, send the same text plain so nothing is ever lost."""
    for payload in ({"parse_mode": "HTML", "text": text[:4000]},
                    {"text": html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]}):
        try:
            r = requests.post(f"{api}/sendMessage", timeout=20, json={
                "chat_id": chat, "disable_web_page_preview": True, **payload})
            if r.ok:
                return True
            log(f"sendMessage {r.status_code}: {r.text[:200]}")
        except requests.RequestException as exc:
            log(f"sendMessage failed: {type(exc).__name__}")
    return False


def _send_resume(api: str, chat: str) -> None:
    from .apply.runner import my_resume
    r = my_resume()
    if not r:
        send(api, chat, "No resume yet. Send me your PDF and every application will use it.")
        return
    try:
        with open(r, "rb") as fh:
            requests.post(f"{api}/sendDocument", timeout=60, data={"chat_id": chat,
                          "caption": f"Every application uploads this ({r.name}). Send a new PDF to replace it."},
                          files={"document": (r.name, fh, "application/pdf")})
    except (OSError, requests.RequestException) as exc:
        send(api, chat, f"couldn't send it: {type(exc).__name__}")


def _document(api: str, tok: str, chat: str, doc: dict) -> None:
    """A PDF you send becomes the resume every application uploads."""
    from .resumes import use
    name = doc.get("file_name") or "file"
    if doc.get("mime_type") != "application/pdf" and not name.lower().endswith(".pdf"):
        send(api, chat, "Send your resume as a PDF to make it the one every application uses.")
        return
    if (doc.get("file_size") or 0) > 5 * 1024 * 1024:
        send(api, chat, "That PDF is over 5 MB: most job sites refuse it. Export a smaller one.")
        return
    try:
        info = requests.get(f"{api}/getFile", params={"file_id": doc["file_id"]}, timeout=20).json()
        data = requests.get(f"https://api.telegram.org/file/bot{tok}/{info['result']['file_path']}", timeout=60).content
    except (requests.RequestException, KeyError, ValueError) as exc:
        send(api, chat, f"couldn't download it ({type(exc).__name__}). Send it again.")
        return
    ok, msg = use(data=data)
    send(api, chat, ("✓ Resume " if ok else "✗ Not saved: ") + _e(msg))
    log(f"resume upload {name}: {'saved' if ok else msg}")


def _reply(api: str, chat: str, text: str) -> None:
    import threading
    cmd = (text or "").strip().lstrip("/").split("_")[0].split()[0].split("@")[0].lower() if text.strip() else ""
    done = threading.Event()

    def typing():                            # Telegram shows "typing…" for ~5 s: repeat until the reply is ready
        while not done.is_set():
            try:
                requests.post(f"{api}/sendChatAction", timeout=10, json={"chat_id": chat, "action": "typing"})
            except requests.RequestException:
                pass
            done.wait(4)
    if cmd in SLOW or cmd not in KNOWN:      # live searches, and a bare company name or a numbered pick
        threading.Thread(target=typing, daemon=True).start()
    t0 = time.time()
    try:
        reply = handle(text, chat=chat, progress=lambda s: send(api, chat, s))
    except Exception as exc:                 # never let one bad command kill the bot
        log(f"handler error on {text[:40]!r}: {exc!r}")
        reply = f"⚠️ error: {_e(type(exc).__name__)}: {_e(str(exc)[:200])}"
    finally:
        done.set()
    if reply == "__SHEET__":
        from .tracker import write
        try:
            x = write()
            with open(x, "rb") as fh:
                requests.post(f"{api}/sendDocument", timeout=60, data={"chat_id": chat,
                              "caption": "Your applications and email replies, updated just now."},
                              files={"document": (x.name, fh,
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        except Exception as exc:
            send(api, chat, f"couldn't make the sheet: {_e(type(exc).__name__)}: {_e(str(exc)[:200])}")
        log(f"{text[:40]!r} → sheet sent")
        return
    if reply == "__RESUME__":
        _send_resume(api, chat)
        log(f"{text[:40]!r} → resume sent")
        return
    parts = reply if isinstance(reply, list) else [reply]       # long replies come as whole pages
    ok = all([send(api, chat, p) for p in parts])
    log(f"{text[:40]!r} → {'sent' if ok else 'NOT SENT'} ({len(parts)} msg) in {time.time() - t0:.1f}s")


def offline_note(since: float, secs: float) -> str | None:
    """One alert when the bot is back after >= 2 h without Telegram. secs is monotonic time, which stands still
    while the laptop sleeps, so only time the laptop was on (and offline) counts."""
    if secs < 2 * 3600:
        return None
    return (f"⚠️ <b>bot offline since {time.strftime('%d %b %H:%M', time.localtime(since))}</b> "
            f"({secs / 3600:.1f} h with the laptop on but Telegram unreachable). Back now; "
            f"anything you sent meanwhile is answered next.")


def _tick() -> None:
    from . import urgent
    try:
        if n := urgent.tick(DB()):
            log(f"sent {n} urgent reminder(s)")
    except Exception as exc:                 # a reminder problem never stops the bot
        log(f"urgent tick failed: {type(exc).__name__}: {exc}")


def run() -> None:
    from concurrent.futures import ThreadPoolExecutor
    tok, chat = env("TELEGRAM_BOT_TOKEN"), str(env("TELEGRAM_CHAT_ID") or "")
    if not tok or not chat:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
    api = f"https://api.telegram.org/bot{tok}"
    from .morning import load_state, save_state
    offset = load_state().get("tg_offset", 0)
    try:
        me = requests.get(f"{api}/getMe", timeout=20).json()
        if not me.get("ok"):
            sys.exit(f"Telegram refused the token: {me.get('description')}. Fix TELEGRAM_BOT_TOKEN in .env.")
        requests.post(f"{api}/deleteWebhook", timeout=20, json={"drop_pending_updates": False})  # webhooks block polling
        requests.post(f"{api}/setMyCommands", timeout=20, json={"commands": [
            {"command": c, "description": d} for c, d in [
                ("today", "jobs posted in the last 3 days"), ("top", "best open jobs"), ("search", "search open jobs: search python"),
                ("company", "a company: every open role + news"),
                ("intel", "a company's hiring pattern from our scans"), ("govt", "govt notices, eligible first"),
                ("eligible", "govt posts you can apply to"), ("deadlines", "last dates in the next 14 days"),
                ("brief", "morning brief now"), ("review", "forms waiting for approval"),
                ("questions", "questions only you can answer"), ("stats", "applied, interviews, response rate"), ("health", "is everything working? fixes"), ("scan", "scan now"),
                ("ping", "is the bot alive"), ("help", "all commands")]]})
        log(f"bot @{me['result'].get('username')} running for chat {chat[-4:].rjust(4, '*')}")
    except requests.RequestException as exc:
        log(f"startup: Telegram unreachable ({type(exc).__name__}); retrying in the loop")
    pool = ThreadPoolExecutor(max_workers=3)   # a slow /company never blocks /help
    fails, down = 0, None                   # down: (wall time, monotonic) of the first failed poll
    ticked = 0.0
    while True:
        if time.time() - ticked > 60:        # 🚨 urgent alerts: 2 h / 6 h reminders, unpin once applied or skipped
            ticked = time.time()
            pool.submit(_tick)
        try:
            r = requests.get(f"{api}/getUpdates", params={"offset": offset, "timeout": 50}, timeout=60).json()
        except (requests.RequestException, ValueError) as exc:
            fails += 1
            down = down or (time.time(), time.monotonic())
            log(f"getUpdates failed ({type(exc).__name__}); retry in {min(60, 5 * fails)}s")
            time.sleep(min(60, 5 * fails))
            continue
        if not r.get("ok"):
            fails += 1
            down = down or (time.time(), time.monotonic())
            log(f"getUpdates refused: {r.get('error_code')} {r.get('description')}")
            if r.get("error_code") == 401:
                sys.exit("Token revoked or wrong: fix TELEGRAM_BOT_TOKEN in .env, then restart the bot.")
            if r.get("error_code") == 409:   # another `hunt bot` or a webhook is polling the same token
                requests.post(f"{api}/deleteWebhook", timeout=20, json={})
            time.sleep(min(60, 5 * fails))
            continue
        if fails:
            log(f"Telegram reachable again after {fails} failed poll(s): answering")
            note = offline_note(down[0], time.monotonic() - down[1])
            if note and send(api, chat, note):
                log("sent the offline alert")
        fails, down = 0, None
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            save_state(tg_offset=offset)
            msg = u.get("message") or u.get("edited_message") or {}
            if str(msg.get("chat", {}).get("id")) != chat:
                log(f"ignored a message from another chat ({str(msg.get('chat', {}).get('id'))[-4:]})")
                continue                     # not you: ignore
            if msg.get("document"):
                pool.submit(_document, api, tok, chat, msg["document"])
                continue
            text = msg.get("text") or ""
            if not text:
                continue
            pool.submit(_reply, api, chat, text)
