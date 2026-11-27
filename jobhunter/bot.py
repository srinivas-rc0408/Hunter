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
  /applied <id>     /interview <id>  /rejected <id>  /ghosted <id>  /offer <id>   update status (or tap a card's buttons)
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
from .notify import job_card
from .db import DB

STATUS_CMDS = {"skip": "skipped", "applied": "applied", "interview": "interview", "rejected": "rejected",
               "offer": "offer", "queue": "queued", "ghosted": "ghosted"}


KNOWN = {"start", "help", "menu", "brief", "top", "today", "new", "govt", "deadlines", "eligible", "search", "health", "review", "stats", "show", "scan", "company", "watch", "intel",
         "questions", "answer", "ping", "form", "fix", "approve", "check", "laptop", "dream", "undream", "dreams", "resume", "mail", "sheet",
         "ref", "outcomes", *STATUS_CMDS}

HELP = """<b>🎯 JobHunter</b> · <b>just type what you want</b>: <i>find ML intern jobs in Bengaluru posted this week</i>,
<i>what does the Birlasoft job need that I don't have?</i>, <i>mark ab12cd34 as interview</i> (a change waits for your ✅ Confirm tap)
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
/skip_&lt;id&gt; · /queue_&lt;id&gt; · /applied_&lt;id&gt; · /interview_&lt;id&gt; · /ghosted_&lt;id&gt; · /offer_&lt;id&gt; (or tap the buttons under a card)

<b>You</b>
/stats · applied today, this week, total, interviews, offers, response rate
/outcomes · response and interview rate by source, role and week (tap the buttons on a job card to record them)
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


from .bot_views import (  # noqa: E402,F401  (re-exported: other modules and tests use bot.<name>)
    _e, mobile_apply, _line, _paged, _today, _stats, _deadlines, _PICK, _COMPANY_PAGES, _clock_ist,
    _choices, _company_cmd, _fact, _company_report, _ref, offline_note)


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
    mine = [j for j in hist if j["status"] in ("applied", "interview", "offer", "rejected", "ghosted")]
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
    if cmd == "outcomes":
        from .outcomes import report
        return report(db)
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
        rows = db.query(status=["applied", "interview", "offer", "rejected", "ghosted"], limit=100000)
        rows = sorted(((db.applied_on(j["id"]) or j.get("status_at") or "", j) for j in rows), key=lambda x: x[0], reverse=True)
        if not rows:
            return "<b>Your applications</b>\nnone yet. Mark one: <code>/applied_&lt;id&gt;</code>"
        mark = {"interview": "🎤", "offer": "🎉", "rejected": "✗", "ghosted": "👻", "applied": "·"}
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




def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)          # → journalctl --user -u jobhunter-bot


def send(api: str, chat: str, text: str, markup: dict | None = None) -> bool:
    """HTML first; if Telegram rejects the HTML, send the same text plain so nothing is ever lost.
    One job card in the message: its outcome buttons come with it."""
    from .outcomes import buttons_for
    kb = markup or buttons_for(text)
    for payload in ({"parse_mode": "HTML", "text": text[:4000]},
                    {"text": html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]}):
        try:
            r = requests.post(f"{api}/sendMessage", timeout=20, json={
                "chat_id": chat, "disable_web_page_preview": True, **payload, **({"reply_markup": kb} if kb else {})})
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
    done = threading.Event()

    def typing():                            # Telegram shows "typing…" for ~5 s: repeat until the reply is ready
        while not done.is_set():
            try:
                requests.post(f"{api}/sendChatAction", timeout=10, json={"chat_id": chat, "action": "typing"})
            except requests.RequestException:
                pass
            done.wait(4)
    threading.Thread(target=typing, daemon=True).start()   # "typing…" the moment any message arrives
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


def _tick(api: str = "", chat: str = "") -> None:
    from . import outcomes, urgent
    try:
        db = DB()
        if n := urgent.tick(db):
            log(f"sent {n} urgent reminder(s)")
        if api and db.state("ghost_check") != str(dt.date.today()):     # once a day: "no reply in 21 days: ghosted?"
            db.set_state("ghost_check", str(dt.date.today()))
            for text, kb in outcomes.ghost_prompts(db):
                send(api, chat, text, kb)
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
            pool.submit(_tick, api, chat)
        try:
            r = requests.get(f"{api}/getUpdates", timeout=60, params={"offset": offset, "timeout": 50,       # buttons too:
                             "allowed_updates": '["message","edited_message","callback_query"]'}).json()
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
            if cq := u.get("callback_query"):          # an outcome button on a job card
                from .outcomes import on_button
                pool.submit(on_button, api, chat, cq)
                continue
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
