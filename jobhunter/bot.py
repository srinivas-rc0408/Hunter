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


KNOWN = {"start", "help", "menu", "brief", "top", "today", "new", "govt", "review", "stats", "show", "scan", "company", "watch",
         "questions", "answer", "ping", "form", "fix", "approve", "check", "dream", "undream", "dreams", "resume",
         *STATUS_CMDS}

HELP = """<b>🎯 JobHunter bot</b>
<b>Every day</b>
/brief · morning summary now
/today · jobs posted in the last 3 days  (next page <code>/today 2</code>)
/top · best open jobs  (<code>/top 5</code>, <code>/top 10</code>, next page <code>/top 10 2</code>)
/review · forms waiting for you
/govt · govt notices and last dates
/stats · your numbers
<b>One job</b> (tap the links in alerts)
/show_&lt;id&gt; · why it matched, link
/form_&lt;id&gt; · every field the bot filled, numbered
/fix_&lt;id&gt; 3 15 days · change field 3 (remembered for every future form)
/approve_&lt;id&gt; · send it as shown (laptop submits it)
/check_&lt;id&gt; · open it on the laptop to read and submit yourself
/skip_&lt;id&gt; · /queue_&lt;id&gt; · hide it / apply to it first
/applied_&lt;id&gt; · /interview_&lt;id&gt; · /rejected_&lt;id&gt; · /offer_&lt;id&gt;
<b>Companies</b>
<code>/dream Zoho</code> · make it a dream company (★, +15, watched)
<code>/undream Zoho</code> · /dreams · list them
<code>/company Aditya Birla</code> · its open jobs with links
<code>/watch Bosch</code> · include it in every scan
<b>Your resume</b>
Send the PDF here: every application uses it from then on
/resume · get the current one
<b>Questions forms asked</b>
/questions · then <code>/answer 1 Yes</code>
<b>Other</b>
/scan · scan now · /ping · is the bot alive
Words work too: <i>help</i>, <i>top 5</i>, <i>dream Zoho</i>."""


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


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
        f_news = ex.submit(companies.news, name)
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
            news, ab = f_news.result(timeout=10), f_about.result(timeout=10)
        except Exception:
            news, ab = [], {}
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
    blr = sum(1 for r in res["listings"] if re.search(r"bengaluru|bangalore", r.get("location") or "", re.I))
    mine = [j for j in db.query(status=["applied", "interview", "offer", "rejected"], limit=100000)
            if companies.same_company(j["company"], info["name"])]
    apps = [f"• {_e(j['title'][:60])} · <b>{j['status']}</b>" + (f" · applied {parse_iso(on):%d %b}" if (on := db.applied_on(j['id'])) else "")
            for j in mine[:8]]
    size = ab.get("employees")
    lines = [f"<b>ℹ️ {_e(info['name'])}</b>",
             f"What it does: {_e(ab.get('description') or info.get('description') or 'unknown')}",
             f"Size: {_e(f'{int(float(size)):,} employees' if size and str(size).replace('.', '').isdigit() else 'unknown')}"
             + (f" · founded {_e(ab['founded'])}" if ab.get("founded") else ""),
             f"Bengaluru office: {'yes, ' + str(blr) + ' open role(s) there' if blr else 'unknown'}",
             f"Dream: {'★ yes' if dreams.find(info['name']) else 'no · <code>/dream ' + _e(info['name']) + '</code>'}",
             "<b>Your applications</b>", *(apps or ["none yet"]),
             f"<b>News, last 30 days</b>"]
    lines += [f"• {n['date'][8:]} {dt.date.fromisoformat(n['date']):%b} · <a href=\"{html.escape(n['url'], quote=True)}\">"
              f"{_e(n['title'][:110])}</a> <i>({_e(n['source'])})</i>" for n in news] or ["none found"]
    for label, site in (("Careers", info.get("careers_url")), ("Website", not info.get("careers_url") and info.get("website"))):
        if site:
            lines.append(f"{label}: <a href=\"{html.escape(site, quote=True)}\">{_e(site[:60])}</a>")
    return [main, "\n".join(lines)]


def handle(text: str, db: DB | None = None) -> str | list[str]:
    """Pure command handler (unit-tested): text in, HTML reply out (a list = several whole messages)."""
    db = db or DB()
    t = (text or "").strip()
    if t.isdigit() and _PICK.get("items") and time.time() - _PICK.get("at", 0) < 900:
        items = _PICK["items"]
        if not 1 <= int(t) <= len(items):
            return f"Pick 1 to {len(items)}."
        return _company_report(items[int(t) - 1], db, watch=_PICK.get("cmd") == "watch")
    if t and not t.startswith("/") and t.split()[0].lower() in KNOWN:
        t = "/" + t                                    # "help", "top 5", "stats" work without the slash
    elif t and not t.startswith("/") and len(t) >= 3 and _choices(t, online=False):
        return _company_cmd(t, db)                     # just a company name: "Zoho", "aditya birla"
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
        head = (f"<b>{_e(j['title'])}</b> · {_e(j['company'])}\n"
                f"{'preview' if app.get('dry') else app.get('outcome')} · {_e(app.get('site'))} · {_e((app.get('at') or '')[:16])}\n"
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
    if cmd in ("approve", "check"):
        j = db.get(jid) if jid else None
        if not j:
            return f"Usage: /{cmd}_&lt;id&gt;"
        import os
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return ("The bot service can't see the laptop screen, so it can't open the browser. On the laptop run "
                    "<code>scripts/install-services.sh</code> once from your desktop session.")
        args = ["apply", j["id"], "--submit" if cmd == "approve" else "--approve"]
        subprocess.Popen([sys.executable, "-m", "jobhunter.cli", *args], cwd=str(config.home()),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, "JH_NOTIFY": "1"})
        return (f"⏳ Sending <b>{_e(j['title'])}</b> · {_e(j['company'])} from the laptop with the values in /form. "
                "You'll get ✅ Applied, or ✋ if the site asks something new." if cmd == "approve" else
                f"⏳ Opening <b>{_e(j['title'])}</b> on the laptop. Read the form there and press Submit on the panel.")
    if cmd == "resume":
        return "__RESUME__"
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
    if cmd == "govt":
        jobs = [j for j in db.query(kind="govt", limit=300) if j["status"] not in ("skipped", "applied")
                and "eligibility:ineligible" not in (j.get("flags") or [])]
        jobs.sort(key=lambda j: j.get("deadline") or "9999")
        return "<b>Govt notices</b>\n" + ("\n".join(_line(j) for j in jobs[:12]) or "none yet")
    if cmd == "review":
        jobs = db.query(status=["needs_you"], limit=20)
        return (f"<b>Waiting for approval: {len(jobs)}</b>\n" + "\n".join(_line(j) for j in jobs)
                + "\nApprove them at localhost:8765 on your laptop.") if jobs else "Nothing waiting. ✅"
    if cmd == "stats":
        n = {k: db.count(w) for k, w in {
            "tracked": "1=1", "open": "status IN ('new','alerted','queued')", "waiting": "status='needs_you'",
            "applied": "status='applied'", "interviews": "status='interview'", "offers": "status='offer'",
            "rejected": "status='rejected'"}.items()}
        return (f"<b>Pipeline</b>\ntracked {n['tracked']} · open {n['open']} · waiting {n['waiting']}\n"
                f"applied {n['applied']} · interviews {n['interviews']} · offers {n['offers']} · "
                f"rejected {n['rejected']}\napplied today {db.applied_today()}")
    if cmd == "show":
        j = db.get(jid) if jid else None
        if not j:
            return "Usage: /show &lt;id&gt;"
        rs = "\n".join("• " + _e(r[:140]) for r in (j.get("reasons") or [])[:8])
        return (f"{_line(j)}\n{_e(j.get('location'))} · status <b>{_e(j['status'])}</b>\n{rs}\n"
                f"<a href=\"{html.escape(j['url'] or '', quote=True)}\">Open posting</a>\n/skip_{j['id'][:8]} · /queue_{j['id'][:8]}")
    if cmd in STATUS_CMDS:
        j = db.get(jid) if jid else None
        if not j:
            return f"Usage: /{cmd} &lt;id&gt;"
        db.set_status(j["id"], STATUS_CMDS[cmd], "telegram")
        return f"✓ {_e(j['title'][:60])} · {_e(j['company'])} → <b>{STATUS_CMDS[cmd]}</b>"
    if cmd == "scan":
        subprocess.Popen([sys.executable, "-m", "jobhunter.cli", "scan"], cwd=str(config.home()),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return "Scan started on the laptop. New matches will arrive as alerts."
    return f"Unknown command /{_e(cmd)}. Send /help"


SLOW = {"today", "brief", "company", "watch", "scan", "top", "govt", "dream", "undream", "approve", "check"}


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
        reply = handle(text)
    except Exception as exc:                 # never let one bad command kill the bot
        log(f"handler error on {text[:40]!r}: {exc!r}")
        reply = f"⚠️ error: {_e(type(exc).__name__)}: {_e(str(exc)[:200])}"
    finally:
        done.set()
    if reply == "__RESUME__":
        _send_resume(api, chat)
        log(f"{text[:40]!r} → resume sent")
        return
    parts = reply if isinstance(reply, list) else [reply]       # long replies come as whole pages
    ok = all([send(api, chat, p) for p in parts])
    log(f"{text[:40]!r} → {'sent' if ok else 'NOT SENT'} ({len(parts)} msg) in {time.time() - t0:.1f}s")


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
                ("brief", "morning brief now"), ("today", "jobs posted in the last 3 days"), ("top", "best open jobs"), ("govt", "govt notices + deadlines"),
                ("review", "forms waiting for approval"), ("questions", "questions only you can answer"),
                ("company", "a company's open listings"), ("stats", "pipeline numbers"), ("scan", "scan now"),
                ("ping", "is the bot alive"), ("help", "all commands")]]})
        log(f"bot @{me['result'].get('username')} running for chat {chat[-4:].rjust(4, '*')}")
    except requests.RequestException as exc:
        log(f"startup: Telegram unreachable ({type(exc).__name__}); retrying in the loop")
    pool = ThreadPoolExecutor(max_workers=3)   # a slow /company never blocks /help
    fails = 0
    while True:
        try:
            r = requests.get(f"{api}/getUpdates", params={"offset": offset, "timeout": 50}, timeout=60).json()
        except (requests.RequestException, ValueError) as exc:
            fails += 1
            log(f"getUpdates failed ({type(exc).__name__}); retry in {min(60, 5 * fails)}s")
            time.sleep(min(60, 5 * fails))
            continue
        if not r.get("ok"):
            fails += 1
            log(f"getUpdates refused: {r.get('error_code')} {r.get('description')}")
            if r.get("error_code") == 401:
                sys.exit("Token revoked or wrong: fix TELEGRAM_BOT_TOKEN in .env, then restart the bot.")
            if r.get("error_code") == 409:   # another `hunt bot` or a webhook is polling the same token
                requests.post(f"{api}/deleteWebhook", timeout=20, json={})
            time.sleep(min(60, 5 * fails))
            continue
        fails = 0
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
